"""Phase 3 D2 — the vision loop: screenshot → vision role → action → screenshot again to check.

The agent uses vision when the page model cannot describe what is on screen: a chart or map drawn on a canvas,
a plugin, a custom widget, or a whole desktop (D1). The tools act on a `Screen`, which is the browser page by
default (`scout.browser.agent_tools.BrowserScreen`) or the disposable desktop (`scout.desktop.DesktopScreen`).

Every action is located by the vision role from a description ("the Download button under the chart"), never
from raw coordinates the planner guesses, and is followed by a fresh screenshot: if nothing changed, the result
says so, so the planner can try another target instead of assuming success.

Gates, enforced here in code for every screen:
- A click whose target, or the element found at the point, looks like it submits or commits something goes
  through the same approval gate as `click_submit` (`approve(summary)`); denied → nothing is clicked.
- Typing is refused for passwords: when the target mentions a password/PIN, or the focused element after the
  click is a password field (the browser can tell; a desktop cannot, so the description rule applies there).
- CAPTCHAs and bot checks are never solved: a target that names one is refused, and when the vision role reports
  one on screen the click is not made. The answer is hand_to_human.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from .browser.agent_tools import SUBMIT_WORDS   # one rule for what counts as submitting

PASSWORD_WORDS = re.compile(r"pass(word|code|phrase)|\bpin\b|κωδικ|passwort|mot de passe|contraseña", re.I)
CAPTCHA_WORDS = re.compile(r"captcha|recaptcha|hcaptcha|turnstile|not a robot|verify (that )?you are (a )?human|"
                           r"bot check|security check|puzzle|select all (the )?(images|squares)", re.I)
COOKIE_WORDS = re.compile(r"cookie|consent|tracking|privacy choices", re.I)
ACCEPT_ALL = re.compile(r"\b(accept|allow|agree)\b.{0,12}\b(all|cookies|tracking)\b", re.I)
KEYS = {"enter", "tab", "escape", "backspace", "delete", "space", "pageup", "pagedown", "home", "end",
        "arrowup", "arrowdown", "arrowleft", "arrowright"}
LOOK_TOKENS = 2048        # the model reasons before answering (AGENTS.md §3 rule 2: never below 512)

LOOK_SYSTEM = ("You read screenshots for a web research agent. Answer only from what is visible. If a CAPTCHA or "
               "bot check is on screen, say so and do not try to solve it. Never reveal or guess passwords.")


class Screen(Protocol):
    """What the vision loop needs from a screen. All calls are synchronous."""
    name: str

    def shot(self) -> bytes: ...                                  # PNG of what is visible now
    def click(self, x: int, y: int) -> None: ...
    def type_text(self, text: str) -> None: ...
    def key(self, key: str) -> None: ...
    def scroll(self, dy: int) -> None: ...
    def at(self, x: int, y: int) -> dict | None: ...              # the element at a point, if the screen knows
    def focused(self) -> dict | None: ...                         # the focused element, if the screen knows
    def state(self) -> dict: ...                                  # what to report after an action (url, title…)


def png_size(png: bytes) -> tuple[int, int]:
    if png[:8] != b"\x89PNG\r\n\x1a\n" or png[12:16] != b"IHDR":
        raise ValueError("not a PNG screenshot")
    return struct.unpack(">II", png[16:24])


def _strip(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()


def parse_json(text: str) -> Any:
    text = _strip(text)
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                return None
    return None


def _image_message(png: bytes, prompt: str) -> list[dict]:
    return [{"role": "system", "content": LOOK_SYSTEM},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}},
                {"type": "text", "text": prompt}]}]


def look(llm, png: bytes, question: str) -> str:
    """Ask the vision role a question about a screenshot."""
    r = llm.chat("vision", _image_message(png, question), max_tokens=LOOK_TOKENS)
    answer = _strip(r.text)
    if not answer:
        raise RuntimeError("the vision model returned an empty answer")
    return answer


def locate(llm, png: bytes, target: str) -> dict:
    """Where to click for `target`: {"x", "y"} in screenshot pixels, or {"missing": reason}, or {"captcha": True}."""
    w, h = png_size(png)
    prompt = (f"The screenshot is {w}x{h} pixels. Find: {target}\n"
              'Reply with only a JSON object: {"x": <int>, "y": <int>, "label": "<the visible text of what you '
              'chose>"} giving the pixel coordinates of its centre; or {"missing": "<why>"} if it is not visible; '
              'or {"captcha": true} if a CAPTCHA or bot check is showing.')
    r = llm.chat("vision", _image_message(png, prompt), max_tokens=LOOK_TOKENS)
    got = parse_json(r.text)
    if not isinstance(got, dict):
        return {"missing": f"the vision model gave no usable position ({_strip(r.text)[:120]!r})"}
    if got.get("captcha"):
        return {"captcha": True}
    if "x" not in got or "y" not in got:
        return {"missing": str(got.get("missing") or "not found")[:200]}
    try:
        x, y = int(round(float(got["x"]))), int(round(float(got["y"])))
    except (TypeError, ValueError):
        return {"missing": f"bad coordinates {got!r}"[:200]}
    if not (0 <= x < w and 0 <= y < h):
        return {"missing": f"the position ({x}, {y}) is outside the {w}x{h} screen"}
    return {"x": x, "y": y, "label": str(got.get("label") or "")[:120]}


@dataclass
class VisionTools:
    """The vision tools on whichever screen is current (`screen()`)."""
    llm: Any
    screen: Callable[[], Screen]
    approve: Callable[[str], bool]
    shots_dir: Path | None = None
    actions: list[dict] | None = None

    def _save(self, png: bytes) -> str | None:
        if self.shots_dir is None:
            return None
        self.shots_dir.mkdir(parents=True, exist_ok=True)
        path = self.shots_dir / f"vision-{len(list(self.shots_dir.glob('vision-*.png'))) + 1}.png"
        path.write_bytes(png)
        return str(path)

    def _after(self, scr: Screen, before: bytes) -> dict:
        after = scr.shot()
        changed = hashlib.sha256(after).digest() != hashlib.sha256(before).digest()
        out: dict[str, Any] = {"screen": scr.name, "changed": changed, **scr.state()}
        if not changed:
            out["note"] = ("The screen did not change after this action: it may have missed. Check with vision_look, "
                           "or describe the target differently.")
        out["screenshot"] = self._save(after)
        return out

    def look(self, a: dict) -> dict:
        scr = self.screen()
        png = scr.shot()
        question = str(a.get("question") or "Describe what is on the screen.")
        answer = look(self.llm, png, question)
        return {"screen": scr.name, "answer": answer, "screenshot": self._save(png), **scr.state()}

    def click(self, a: dict) -> dict:
        target = str(a.get("target") or "").strip()
        if not target:
            return {"error": "describe the target to click (its visible text and where it is)"}
        if CAPTCHA_WORDS.search(target):
            return {"error": "Scout never solves CAPTCHAs or bot checks: use hand_to_human."}
        scr = self.screen()
        before = scr.shot()
        where = locate(self.llm, before, target)
        if where.get("captcha"):
            return {"error": "a CAPTCHA or bot check is on screen: Scout never solves it. Use hand_to_human.",
                    "screenshot": self._save(before)}
        if "missing" in where:
            return {"error": f"could not find {target!r} on screen: {where['missing']}. Scroll, or look first.",
                    "screenshot": self._save(before)}
        x, y, label = where["x"], where["y"], where["label"]
        el = scr.at(x, y) or {}
        if el.get("password"):
            return {"error": "that is a password field: Scout never enters passwords. Use hand_to_human."}
        if CAPTCHA_WORDS.search(label) or el.get("captcha"):
            return {"error": "that is a CAPTCHA or bot check: Scout never solves it. Use hand_to_human."}
        names = " ".join(filter(None, [target, label, el.get("label")]))
        if self._accepts_cookies(target, label or el.get("label") or ""):
            return {"error": "Scout never accepts cookies or tracking for the user: decline non-essential cookies "
                             "(\"Reject all\", \"Necessary only\"), or get the content another way."}
        decision = None
        if el.get("submit_like") or SUBMIT_WORDS.search(names):
            summary = str(a.get("action_summary") or f'Click "{label or target}" on {scr.state().get("url", scr.name)}')
            if not self.approve(summary):
                return {"decision": "denied", "note": "The human did not approve; nothing was clicked."}
            decision = "approved"
        scr.click(x, y)
        out = {"clicked": {"x": x, "y": y, "label": label, **({"element": el.get("label")} if el else {})},
               **self._after(scr, before)}
        if decision:
            out["decision"] = decision
        self._record("vision_click", {"target": target}, label)
        return out

    @staticmethod
    def _accepts_cookies(target: str, label: str) -> bool:
        from .browser.consent import is_reject_label
        if is_reject_label(label) or (not label and is_reject_label(target)):
            return False
        if ACCEPT_ALL.search(label or target):
            return True
        return bool(COOKIE_WORDS.search(target + " " + label)
                    and re.search(r"\b(accept|allow|agree|ok|got it|continue)\b", label or target, re.I))

    def _password_on_screen(self, png: bytes, x: int, y: int) -> bool:
        """A screen that cannot see inside the page (the desktop): ask the vision role about the field."""
        answer = look(self.llm, png, f"Is the input field at pixel ({x}, {y}) a password, PIN or secret-code field "
                                     "(masked characters, or labelled password, PIN or passcode)? Reply yes or no.")
        return answer.strip().lower().startswith("yes")

    def type(self, a: dict) -> dict:
        target, text = str(a.get("target") or "").strip(), str(a.get("text") or "")
        if PASSWORD_WORDS.search(target):
            return {"error": "Scout never types passwords or PINs: use hand_to_human so the person signs in."}
        if CAPTCHA_WORDS.search(target):
            return {"error": "Scout never solves CAPTCHAs or bot checks: use hand_to_human."}
        scr = self.screen()
        before = scr.shot()
        if target:
            where = locate(self.llm, before, target)
            if where.get("captcha"):
                return {"error": "a CAPTCHA or bot check is on screen: Scout never solves it. Use hand_to_human."}
            if "missing" in where:
                return {"error": f"could not find {target!r} on screen: {where['missing']}"}
            el = scr.at(where["x"], where["y"])
            if (el or {}).get("password") or PASSWORD_WORDS.search(where.get("label", "")) or (
                    el is None and self._password_on_screen(before, where["x"], where["y"])):
                return {"error": "that is a password field: Scout never enters passwords. Use hand_to_human."}
            scr.click(where["x"], where["y"])
        elif scr.focused() is None:
            return {"error": "on this screen Scout cannot tell which field has focus: describe the field (target)."}
        focus = scr.focused() or {}
        if focus.get("password"):
            return {"error": "the focused field is a password field: nothing was typed. Use hand_to_human."}
        scr.type_text(text)
        self._record("vision_type", {"target": target, "text": text}, target)
        return {"typed": len(text), **self._after(scr, before)}

    def key(self, a: dict) -> dict:
        k = str(a.get("key") or "").strip().lower().replace(" ", "")
        if k not in KEYS:
            return {"error": f"unknown key {a.get('key')!r}; use one of {sorted(KEYS)}"}
        scr = self.screen()
        before = scr.shot()
        if k == "enter":            # Enter can submit a form: same gate as a submit click
            el = scr.focused()
            if (el or {}).get("password"):
                return {"error": "the focused field is a password field: Scout does not sign in. Use hand_to_human."}
            if el is None or el.get("in_form") or el.get("submit_like"):    # a screen that cannot tell: ask
                summary = str(a.get("action_summary") or f"Press Enter to submit the form on "
                                                          f"{scr.state().get('url', scr.name)}")
                if not self.approve(summary):
                    return {"decision": "denied", "note": "The human did not approve; Enter was not pressed."}
        scr.key(k)
        self._record("vision_key", {"key": k}, None)
        return self._after(scr, before)

    def scroll(self, a: dict) -> dict:
        scr = self.screen()
        before = scr.shot()
        scr.scroll(-700 if a.get("direction") == "up" else 700)
        return self._after(scr, before)

    def _record(self, tool: str, args: dict, label: str | None) -> None:
        if self.actions is not None:
            self.actions.append({"tool": tool, "args": args, "label": label, "kind": "vision"})

    def call(self, name: str, a: dict) -> dict:
        fn = {"vision_look": self.look, "vision_click": self.click, "vision_type": self.type,
              "vision_key": self.key, "vision_scroll": self.scroll}[name]
        return fn(a)


def _tool(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": list(properties) if required is None else required}}}


VISION_TOOLS = [
    _tool("vision_look", "Look at the screen (the browser page, or the desktop when one is open) and answer a "
          "question about it: read a chart or map drawn on a canvas, a picture, a custom widget. Use when the page's "
          "element list and text do not show what you need.", {"question": {"type": "string"}}),
    _tool("vision_click", "Click something you describe by what it looks like and where it is (\"the blue Download "
          "button under the chart\"); Scout finds it on a screenshot. For things the element list does not show. "
          "Clicks that submit or commit something ask the human first.",
          {"target": {"type": "string"},
           "action_summary": {"type": "string", "description": "If it submits or commits: what will happen."}},
          ["target"]),
    _tool("vision_type", "Click a field you describe, then type text into it. Never passwords.",
          {"target": {"type": "string"}, "text": {"type": "string"}}),
    _tool("vision_key", "Press one key on the screen: enter, tab, escape, backspace, delete, space, pageup, "
          "pagedown, home, end, arrowup/down/left/right. Enter in a form asks the human first.",
          {"key": {"type": "string"}, "action_summary": {"type": "string"}}, ["key"]),
    _tool("vision_scroll", "Scroll the screen.", {"direction": {"type": "string", "enum": ["down", "up"]}}, []),
]
VISION_TOOL_NAMES = {t["function"]["name"] for t in VISION_TOOLS}
