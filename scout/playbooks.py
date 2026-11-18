"""B5 — playbooks: known site flows as YAML steps, run by Scout without a model turn per action.

A playbook names the pages it applies to, the values it needs from the objective (params), and its steps:

    name: httpbin-pizza
    description: Order a pizza on httpbin's demo form.
    match:
      urls: ["httpbin.org/forms/post"]       # any of these in the objective selects the playbook
      words: []                              # or: all of these words in the objective
    params:
      custname: {description: "customer name", required: true}
      toppings: {description: "toppings", type: list}
    steps:
      - open: https://httpbin.org/forms/post
      - type: {field: "Customer name", text: "{custname}"}
      - check: {field: "{item}"}
        for_each: toppings
      - submit: {element: "Submit order", summary: "Submit the pizza order for {custname}"}
      - expect: {text: "custname"}
      - fuzzy: "Report what the site returned."

Step kinds: open, type, select, check (tick a checkbox or radio if it is not ticked), click, submit (through the
approval gate: click_submit), expect (text on the page), wait (seconds), fuzzy (an instruction for the model).
Any step may say `optional: true` (a failure is skipped). Elements are found by their label on the current page,
not by selectors, so small page changes do not break a playbook.

Every action goes through the same browser tools as the model's own calls, so the gates hold: submits are
approved first, password fields are refused. When a step fails, the run stops and returns the page as it is,
with the steps still to do: the model continues by hand (the "fuzzy" fallback). A `fuzzy` step always hands
over to the model.

Traces (for promotion into playbooks, D4): the browser actions of a successful task are written to the task
folder as `trace.yaml` in this same step format (see `trace_from_actions`).
"""

from __future__ import annotations

import os
import re
import string
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from .config import ROOT

STEP_KINDS = ("open", "type", "select", "check", "click", "submit", "expect", "wait", "fuzzy")
TEXT_INPUTS = {None, "text", "email", "tel", "url", "search", "number", "date", "time", "datetime-local", "month",
               "week", "color", "range"}


class PlaybookError(ValueError):
    pass


@dataclass
class Playbook:
    name: str
    description: str = ""
    urls: list[str] = field(default_factory=list)
    words: list[str] = field(default_factory=list)
    params: dict[str, dict] = field(default_factory=dict)
    steps: list[dict] = field(default_factory=list)
    path: str = ""

    def matches(self, objective: str) -> bool:
        o = objective.lower()
        if any(u.lower() in o for u in self.urls):
            return True
        return bool(self.words) and all(w.lower() in o for w in self.words)

    def for_model(self) -> dict:
        return {"name": self.name, "description": self.description,
                "params": {k: {kk: vv for kk, vv in v.items() if kk in ("description", "required", "type", "enum")}
                           for k, v in self.params.items()}}


def playbooks_dir() -> Path:
    return Path(os.environ.get("SCOUT_PLAYBOOKS_DIR") or ROOT / "playbooks").expanduser()


def parse_playbook(data: dict, path: str = "") -> Playbook:
    where = path or "playbook"
    if not isinstance(data, dict) or not data.get("name"):
        raise PlaybookError(f"{where}: needs a `name`")
    steps = data.get("steps") or []
    if not isinstance(steps, list) or not steps:
        raise PlaybookError(f"{where}: needs a list of `steps`")
    for i, s in enumerate(steps, 1):
        kinds = [k for k in s if k in STEP_KINDS] if isinstance(s, dict) else []
        if len(kinds) != 1:
            raise PlaybookError(f"{where}: step {i} must have exactly one of {', '.join(STEP_KINDS)}: {s!r}")
        if s.get("for_each") and s["for_each"] not in (data.get("params") or {}):
            raise PlaybookError(f"{where}: step {i} for_each names an unknown param {s['for_each']!r}")
    m = data.get("match") or {}
    params = {k: (v if isinstance(v, dict) else {"description": str(v)}) for k, v in (data.get("params") or {}).items()}
    return Playbook(name=str(data["name"]), description=str(data.get("description") or ""),
                    urls=list(m.get("urls") or []), words=list(m.get("words") or []), params=params,
                    steps=steps, path=path)


def load_playbooks(directory: Path | None = None) -> list[Playbook]:
    d = directory or playbooks_dir()
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.yaml")):
        out.append(parse_playbook(yaml.safe_load(p.read_text()), str(p)))
    return out


def find_playbook(name: str, directory: Path | None = None) -> Playbook | None:
    return next((p for p in load_playbooks(directory) if p.name == name), None)


def matching(objective: str, directory: Path | None = None) -> list[Playbook]:
    return [p for p in load_playbooks(directory) if p.matches(objective)]


# -- finding elements by label ------------------------------------------------------------------------------

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[:*]+\s*$", "", (s or "").strip())).lower()


def _fits(el, kind: str) -> bool:
    if kind == "type":
        return (el.tag == "textarea" or (el.tag == "input" and el.type in TEXT_INPUTS)
                or el.role == "textbox") and el.type != "password"
    if kind == "select":
        return el.tag == "select" or el.role in ("combobox", "listbox")
    if kind == "check":
        return (el.tag == "input" and el.type in ("checkbox", "radio")) or el.role in ("checkbox", "radio", "switch")
    return el.tag in ("button", "a", "summary") or el.role in ("button", "link", "tab", "menuitem") or (
        el.tag == "input" and el.type in ("submit", "button", "image", "reset"))


def find_element(view, label: str, kind: str):
    """The best element on the page for a label: exact, then prefix, then contained either way."""
    want = _norm(label)
    if not want or view is None:
        return None
    cands = [e for e in view.elements if _fits(e, kind) and not e.disabled]
    for test in (lambda n: n == want, lambda n: n.startswith(want), lambda n: want in n,
                 lambda n: len(n) >= 3 and n in want):
        hit = [e for e in cands if e.name and test(_norm(e.name))]
        if hit:
            return hit[0]
    return None


# -- running ------------------------------------------------------------------------------------------------

class _Values(dict):
    def __missing__(self, key):
        raise PlaybookError(f"no value for {{{key}}}")


def fill(template: Any, values: dict) -> Any:
    if isinstance(template, str):
        return string.Formatter().vformat(template, (), _Values(values))
    if isinstance(template, dict):
        return {k: fill(v, values) for k, v in template.items()}
    return template


def _fields(template: Any) -> set[str]:
    """The {names} a step's template refers to."""
    if isinstance(template, str):
        return {f for _, f, _, _ in string.Formatter().parse(template) if f}
    if isinstance(template, dict):
        return set().union(*(_fields(v) for v in template.values())) if template else set()
    return set()


def check_params(pb: Playbook, params: dict) -> list[str]:
    missing = [k for k, spec in pb.params.items() if spec.get("required") and params.get(k) in (None, "", [])]
    return missing


def run_playbook(pb: Playbook, params: dict, call: Callable[[str, dict], Any], view: Callable[[], Any],
                 sleep: Callable[[float], None] = time.sleep) -> dict:
    """Run the steps through `call(tool, args)` (the agent's browser calls, gates included). `view()` returns the
    current PageView. Stops at the first failed step or at a fuzzy step, and says what is left."""
    params = dict(params or {})
    missing = check_params(pb, params)
    if missing:
        return {"playbook": pb.name, "error": f"missing params: {', '.join(missing)}", "params": pb.for_model()["params"]}
    for k, spec in pb.params.items():
        params.setdefault(k, [] if spec.get("type") == "list" else "")
    log: list[str] = []
    last: Any = None
    expanded: list[tuple[int, dict, dict]] = []                   # (step number, step, values)
    for i, s in enumerate(pb.steps, 1):
        if s.get("for_each"):
            items = params.get(s["for_each"]) or []
            items = [items] if isinstance(items, str) else list(items)
            expanded += [(i, s, {**params, "item": it}) for it in items]
        else:
            expanded.append((i, s, params))

    def stop(n: int, step: dict, error: str, done: int) -> dict:
        left = [st for (k, st, _) in expanded[done:]]
        page = view()
        return {"playbook": pb.name, "completed": done, "of": len(expanded), "log": log,
                "failed_step": {"n": n, "step": step, "error": error},
                "next": "The playbook stopped here. Continue by hand from this page (the steps left are listed); "
                        "do not start over.",
                "steps_left": left[:12], **({"page": page.for_model(1500)} if page is not None else {})}

    for done, (n, step, values) in enumerate(expanded):
        kind = next(k for k in step if k in STEP_KINDS)
        if step.get("optional") and any(values.get(f) in (None, "", []) for f in _fields(step[kind])):
            log.append(f"{n}. {kind} skipped: no value")
            continue
        try:
            arg = fill(step[kind], values)
        except PlaybookError as e:
            return stop(n, step, str(e), done)
        if kind == "fuzzy":
            page = view()
            return {"playbook": pb.name, "completed": done, "of": len(expanded), "log": log,
                    "next": str(arg), "steps_left": [st for (_, st, _) in expanded[done + 1:]][:12],
                    **({"page": page.for_model(3000)} if page is not None else {})}
        error = None
        if kind == "open":
            last = call("browser_open", {"url": arg if isinstance(arg, str) else arg["url"]})
        elif kind == "wait":
            sleep(float(arg if not isinstance(arg, dict) else arg.get("seconds", 1)))
            last = call("browser_read", {})
        elif kind == "expect":
            text = arg if isinstance(arg, str) else arg["text"]
            v = view()
            hay = ((v.text if v else "") + " " + " ".join(e.name for e in (v.elements if v else []))).lower()
            if text.lower() not in hay:
                error = f"expected {text!r} on the page"
            last = {"ok": error is None}
        else:
            a = arg if isinstance(arg, dict) else {"field": arg, "element": arg}
            label = a.get("field") or a.get("element") or ""
            el = find_element(view(), label, {"submit": "click"}.get(kind, kind))
            if el is None:
                error = f"no {kind} element labelled {label!r} on the page"
            elif kind == "type":
                last = call("browser_type", {"element_id": el.id, "text": str(a.get("text", ""))})
            elif kind == "select":
                last = call("browser_select", {"element_id": el.id, "option": str(a.get("option", ""))})
            elif kind == "check":
                last = {"ok": True, "note": "already ticked"} if el.checked else \
                    call("browser_click", {"element_id": el.id})
            elif kind == "click":
                last = call("browser_click", {"element_id": el.id})
            elif kind == "submit":
                last = call("click_submit", {"element_id": el.id,
                                             "action_summary": a.get("summary") or f'Click "{el.name}"'})
                if isinstance(last, dict) and last.get("decision") == "denied":
                    error = "the approval was denied: nothing was submitted"
        if error is None and isinstance(last, dict) and last.get("error"):
            error = str(last["error"])
        if error:
            if step.get("optional"):
                log.append(f"{n}. {kind} skipped (optional): {error}")
                continue
            return stop(n, step, error, done)
        log.append(f"{n}. {kind} {_brief(arg)}: ok")
    page = view()
    return {"playbook": pb.name, "done": True, "completed": len(expanded), "of": len(expanded), "log": log,
            **({"page": page.for_model(3000)} if page is not None else {})}


def _brief(arg: Any) -> str:
    if isinstance(arg, dict):
        arg = arg.get("field") or arg.get("element") or arg.get("url") or arg.get("text") or ""
    return repr(str(arg)[:60])


# -- traces ---------------------------------------------------------------------------------------------------

def trace_from_actions(actions: list[dict], objective: str) -> dict:
    """A draft playbook from the browser actions of a successful task (values kept literal; D4 turns repeated
    traces into reviewed playbooks with params)."""
    steps: list[dict] = []
    for a in actions:
        t, label = a["tool"], a.get("label") or ""
        if t == "browser_open":
            steps.append({"open": a["args"]["url"]})
        elif t == "browser_type":
            steps.append({"type": {"field": label, "text": a["args"].get("text", "")}})
        elif t == "browser_select":
            steps.append({"select": {"field": label, "option": a["args"].get("option", "")}})
        elif t == "browser_click":
            steps.append({"check" if a.get("kind") in ("checkbox", "radio") else "click":
                          {"field" if a.get("kind") in ("checkbox", "radio") else "element": label}})
        elif t == "click_submit":
            steps.append({"submit": {"element": label, "summary": a["args"].get("action_summary", "")}})
        elif t == "browser_download":
            steps.append({"fuzzy": f'Download the file behind "{label}" with browser_download.'})
    urls = [a["args"]["url"] for a in actions if a["tool"] == "browser_open"]
    first = re.sub(r"^https?://", "", urls[0]).split("?")[0] if urls else ""
    return {"name": "draft-" + re.sub(r"[^a-z0-9]+", "-", first.lower()).strip("-")[:60],
            "description": f"Trace of a successful run: {objective[:200]}",
            "match": {"urls": [first] if first else []}, "params": {}, "steps": steps}


def write_trace(path: Path, actions: list[dict], objective: str) -> Path | None:
    if not actions:
        return None
    path.write_text(yaml.safe_dump(trace_from_actions(actions, objective), sort_keys=False, allow_unicode=True))
    return path
