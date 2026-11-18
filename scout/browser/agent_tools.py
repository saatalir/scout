"""Phase 2 B2-B4: browser tools for the agent, with resilience and the human gates enforced in code.

The agent loop is synchronous; Playwright is async. BrowserRunner keeps one
browser session (the task's profile: the Tor profile when the task's Tor choice
is on) open on its own event-loop thread for the whole task and runs each tool
call there.

Gates (B4), enforced here, never left to the prompt:
- `click_submit` is the only way to submit a form or press an irreversible
  button. It always asks the human first (logged as its own request_approval
  step); `browser_click` refuses submit-like elements and points to it.
- `browser_type` refuses password fields: sign-ins go to the human.
Resilience (B3): navigation is retried on timeouts and network errors; after a
click the page is re-read; a stale or unknown element id gets a clear "re-read
the page" error rather than a crash. A link or script that opens a new tab or
popup is followed (browser_back returns to the opener). JavaScript dialogs are
answered safely: alerts acknowledged, confirms and prompts declined, except
during an approved click_submit, whose confirmation dialog is accepted.

Downloads (B2): browser_download clicks an element and catches the browser's
own download, so a signed-in session's files can be fetched; the file is
streamed back from the VM and passes the same checks as download_file. A URL is
never fetched by Playwright's request API: that would leave from this machine,
not the browser's network (and not through Tor).
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Awaitable, Callable

from playwright.async_api import Error as PWError
from playwright.async_api import TimeoutError as PWTimeout

from ..tools import web
from .config import BrowserConfig, load_browser_config
from .page import PageView, locator_for, snapshot
from .session import open_browser

SUBMIT_WORDS = re.compile(
    r"\b(submit|send|pay|purchase|buy|order|checkout|check out|confirm|apply|sign up|register|delete|remove|"
    r"publish|post|book|reserve|subscribe|place order|transfer|accept terms)\b", re.I)
NAV_TIMEOUT_MS = 45000
DOWNLOAD_WAIT_S = 60          # a download can take a while to start (generated reports)


def _tool(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": list(properties) if required is None else required}}}


BROWSER_TOOLS = [
    _tool("browser_open", "Tier 3. Open a URL in the real browser and return the page: its interactive elements "
          "(with ids like e7), main text and forms. Use when fetch_page is not enough (forms, logins, JavaScript).",
          {"url": {"type": "string"}}),
    _tool("browser_read", "Re-read the current browser page (after it changed).", {}, []),
    _tool("browser_click", "Click an element by id (links, tabs, buttons that do not submit or commit anything).",
          {"element_id": {"type": "string"}}),
    _tool("browser_type", "Type text into an input by id (replaces its content). Never for passwords.",
          {"element_id": {"type": "string"}, "text": {"type": "string"}}),
    _tool("browser_select", "Choose an option in a dropdown by id and the option's visible text.",
          {"element_id": {"type": "string"}, "option": {"type": "string"}}),
    _tool("browser_scroll", "Scroll the page to reveal more elements.",
          {"direction": {"type": "string", "enum": ["down", "up"]}}, []),
    _tool("click_submit", "Submit a form or press a button that commits something (submit, send, pay, order, "
          "apply, delete…). Scout asks the human for approval first; if denied, nothing is clicked.",
          {"element_id": {"type": "string"},
           "action_summary": {"type": "string", "description": "What will happen, for the human to approve."}}),
    _tool("browser_download", "Click a download link or button by id and keep the file the browser downloads "
          "(works in a signed-in session). The file is verified like download_file; pass its path to "
          "save_to_library. For a plain public URL use download_file instead.",
          {"element_id": {"type": "string"},
           "expected_kind": {"type": "string", "enum": ["pdf", "zip", "csv", "json", "any"]},
           "expected_title": {"type": "string", "description": "For a PDF: its title, to check it is the right file."},
           "filename": {"type": "string"}}, ["element_id"]),
    _tool("browser_back", "Go back: to the previous page, or close a tab or popup the browser opened and return "
          "to the page that opened it.", {}, []),
    _tool("browser_screenshot", "Save a screenshot of the current page to the task folder (for the record or the "
          "human; you cannot see images).", {}, []),
]
BROWSER_TOOL_NAMES = {t["function"]["name"] for t in BROWSER_TOOLS}


class BrowserRunner:
    """One browser session for a task, on its own event-loop thread."""

    def __init__(self, profile: str, config: BrowserConfig | None = None,
                 setup: Callable[[Any], Awaitable[None]] | None = None):
        self.profile, self.config, self.setup = profile, config, setup
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="scout-browser", daemon=True)
        self.thread.start()
        self.stack: AsyncExitStack | None = None
        self.session = None
        self.page = None
        self.view: PageView | None = None
        self.downloads_dir: Path | None = None      # set by the agent: the task's downloads folder
        self.screens_dir: Path | None = None
        self.opener: dict[Any, Any] = {}             # tab or popup -> the page that opened it
        self._popups: list[Any] = []
        self._downloads: list[Any] = []
        self._watched: set[int] = set()
        self.dialogs: list[str] = []
        self.accept_confirms = False

    def _watch(self, page) -> None:
        """Listen for popups, dialogs and downloads on a page, and at once on every popup it opens (a link may
        open a tab that only downloads a file)."""
        if id(page) in self._watched:
            return
        self._watched.add(id(page))

        def on_popup(p):
            self._popups.append((page, p))
            self._watch(p)
        page.on("popup", on_popup)
        page.on("dialog", self._on_dialog)
        page.on("download", lambda d: self._downloads.append(d))

    async def _on_dialog(self, dialog) -> None:
        kind, msg = dialog.type, (dialog.message or "")[:200]
        try:
            if kind in ("alert", "beforeunload") or (kind == "confirm" and self.accept_confirms):
                await dialog.accept()
                self.dialogs.append(f"{kind} accepted: {msg}")
            else:
                await dialog.dismiss()               # never confirm something on the agent's own initiative
                self.dialogs.append(f"{kind} declined: {msg}")
        except PWError:
            pass

    @property
    def live_view(self) -> str | None:
        return self.session.live_view if self.session is not None else None

    def run(self, coro: Awaitable[Any], timeout: float = 180) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    async def _ensure(self):
        if self.page is None or self.page.is_closed():
            if self.session is None:
                self.stack = AsyncExitStack()
                self.session = await self.stack.enter_async_context(
                    open_browser(self.profile, self.config or load_browser_config()))
                if self.setup:
                    await self.setup(self.session.context)
            self.page = await self.session.new_page()
            self._watch(self.page)
        return self.page

    async def _follow_popup(self, out: dict) -> dict:
        """B3: a click opened a new tab or popup: continue there (the opener stays open for browser_back)."""
        if not self._popups or "error" in out:
            return out
        opener, popup = self._popups.pop()
        self._popups.clear()
        if popup.is_closed():
            return out
        self.opener[popup] = opener
        self._watch(popup)
        self.page = popup
        try:
            await popup.wait_for_load_state("domcontentloaded", timeout=15000)
        except PWTimeout:
            pass
        return {"note": "The click opened a new tab or popup; this is it. browser_back returns to the previous page.",
                **await self._read()}

    async def _read(self) -> dict:
        page = await self._ensure()
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10000)
        except PWTimeout:
            pass
        self.view = await snapshot(page)
        out = self.view.for_model()
        if self.dialogs:                               # first, so it is seen (and kept in shortened summaries)
            out = {"dialogs": self.dialogs[:], **out}
            self.dialogs.clear()
        return out

    async def _goto(self, url: str) -> dict:
        page = await self._ensure()
        last: Exception | None = None
        for attempt in range(3):                      # B3: navigation retries
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                out = await self._read()
                if resp is not None and resp.status >= 400:
                    out["http_status"] = resp.status
                return out
            except (PWTimeout, PWError) as e:
                last = e
                if "net::ERR_NAME_NOT_RESOLVED" in str(e) or "Invalid URL" in str(e):
                    break                              # permanent
                await asyncio.sleep(2 * (attempt + 1))
        return {"error": f"could not open {url}: {type(last).__name__}: {str(last).splitlines()[0][:200]}"}

    def _known(self, eid: str):
        if self.view is None:
            return None, {"error": "no page is open: use browser_open first"}
        el = self.view.element(eid)
        if el is None:
            return None, {"error": f"unknown element id {eid!r} on this page: call browser_read and use a current id"}
        return el, None

    async def _act(self, eid: str, action: Callable[[Any], Awaitable[None]]) -> dict:
        page = await self._ensure()
        loc = locator_for(page, eid)
        try:
            if await loc.count() == 0:
                return {"error": f"element {eid} is no longer on the page (it changed): call browser_read"}
            await action(loc)
        except PWTimeout:
            return {"error": f"element {eid} did not respond in time (covered or disabled?); re-read the page, "
                             "close any banner or dialog, or try another element"}
        except PWError as e:
            return {"error": f"browser error on {eid}: {str(e).splitlines()[0][:200]}"}
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
        except PWTimeout:
            pass
        await asyncio.sleep(0.5)                       # let script-driven changes settle
        return await self._follow_popup(await self._read())

    def is_submit_like(self, eid: str) -> bool:
        el = self.view.element(eid) if self.view else None
        if el is None:
            return False
        if el.tag in ("button",) and el.form and el.type in (None, "submit"):
            return True
        if el.tag == "input" and el.type in ("submit", "image"):
            return True
        return bool(SUBMIT_WORDS.search(el.name or ""))

    # -- tool entry points (sync) ------------------------------------------------------------------
    def call(self, name: str, a: dict, approve: Callable[[str], bool]) -> Any:
        if name == "browser_open":
            return self.run(self._goto(a["url"]))
        if name == "browser_read":
            return self.run(self._read())
        if name == "browser_back":
            return self.run(self._back())
        if name == "browser_screenshot":
            return self.run(self._screenshot())
        if name == "browser_scroll":
            sign = -1 if a.get("direction") == "up" else 1
            return self.run(self._scroll(sign))
        eid = str(a.get("element_id", "")).strip()
        el, err = self._known(eid)
        if err:
            return err
        if name == "browser_click":
            if self.is_submit_like(eid):
                return {"error": f'"{el.name}" looks like it submits or commits something: use click_submit, '
                                 "which asks the human first"}
            return self.run(self._act(eid, lambda loc: loc.click(timeout=15000)))
        if name == "browser_type":
            if el.type == "password":
                return {"error": "this is a password field: Scout never enters passwords. Use hand_to_human so the "
                                 "person signs in through the live view."}
            return self.run(self._act(eid, lambda loc: loc.fill(str(a.get("text", "")), timeout=15000)))
        if name == "browser_select":
            option = str(a.get("option", ""))
            if el.options and option not in el.options:
                close = [o for o in el.options if option.lower() in o.lower()]
                if len(close) == 1:
                    option = close[0]
                else:
                    return {"error": f"option {option!r} not in {el.options[:15]}"}
            return self.run(self._act(eid, lambda loc: loc.select_option(label=option, timeout=15000)))
        if name == "browser_download":
            if self.is_submit_like(eid):
                return {"error": f'"{el.name}" looks like it submits or commits something: use click_submit'}
            return self.run(self._download(eid, a), timeout=240)
        if name == "click_submit":
            summary = str(a.get("action_summary") or f'Click "{el.name}" on {self.view.url}')
            if not approve(summary):
                return {"decision": "denied", "note": "The human did not approve; nothing was clicked."}
            self.accept_confirms = True                # the approved action's own "are you sure?" dialog
            try:
                out = self.run(self._act(eid, lambda loc: loc.click(timeout=15000)))
            finally:
                self.accept_confirms = False
            if isinstance(out, dict) and "error" not in out:
                out["decision"] = "approved"
            return out
        raise ValueError(f"unknown browser tool {name!r}")

    async def _download(self, eid: str, a: dict) -> Any:
        """Returns a web.Download (verified or not), or an error dict."""
        page = await self._ensure()
        loc = locator_for(page, eid)
        if await loc.count() == 0:
            return {"error": f"element {eid} is no longer on the page (it changed): call browser_read"}
        before = page.url
        self._downloads.clear()
        try:
            await loc.click(timeout=15000)
        except PWTimeout:
            return {"error": f"element {eid} did not respond in time (covered or disabled?); re-read the page"}
        except PWError as e:
            return {"error": f"browser error on {eid}: {str(e).splitlines()[0][:200]}"}
        t0 = time.monotonic()
        while not self._downloads and time.monotonic() - t0 < DOWNLOAD_WAIT_S:
            await asyncio.sleep(0.25)
            opened = page.url != before or any(not p.is_closed() for _, p in self._popups)
            if opened and time.monotonic() - t0 > 5:      # a normal page opened: no download is coming
                break
        if not self._downloads:
            out = await self._follow_popup(await self._read())
            url = self.page.url if self.page else before
            hint = (f" The browser opened {url} instead; if that is the file, use download_file with that URL."
                    if url != before else "")
            return {"error": f"clicking {eid} started no download.{hint}", **out}
        dl = self._downloads.pop(0)
        self._popups.clear()                           # a tab opened only for the download is not followed
        dest = self.downloads_dir or Path(".")
        dest.mkdir(parents=True, exist_ok=True)
        tmp = dest / f".browser-download-{int(time.time() * 1000)}"
        try:
            await dl.save_as(tmp)                      # streamed back from the VM when the browser is remote
            data = tmp.read_bytes()
        except PWError as e:
            return {"error": f"the download failed: {str(e).splitlines()[0][:200]}"}
        finally:
            tmp.unlink(missing_ok=True)
        kind = web.sniff_kind(data[:4096], "")
        name = web.safe_name(a.get("filename") or dl.suggested_filename, kind)
        return web.verify_bytes(data, dl.url, "", name, dest, kind=kind, expected_title=a.get("expected_title"),
                                expected_kind=a.get("expected_kind"))

    async def _back(self) -> dict:
        page = await self._ensure()
        opener = self.opener.pop(page, None)
        if opener is not None and not opener.is_closed():
            await page.close()
            self.page = opener
            return {"note": "Closed the tab or popup; back on the page that opened it.", **await self._read()}
        resp = await page.go_back(wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        if resp is None and page.url in ("about:blank", ""):
            return {"error": "there is no previous page"}
        return await self._read()

    async def _screenshot(self) -> dict:
        page = await self._ensure()
        d = self.screens_dir or self.downloads_dir or Path(".")
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"screenshot-{len(list(d.glob('screenshot-*.png'))) + 1}.png"
        await page.screenshot(path=str(path))
        return {"screenshot": str(path), "url": page.url}

    async def _scroll(self, sign: int) -> dict:
        page = await self._ensure()
        await page.mouse.wheel(0, sign * 900)
        await asyncio.sleep(0.4)
        return await self._read()

    def close(self) -> None:
        async def _close():
            if self.stack:
                await self.stack.aclose()
        try:
            if self.session is not None:
                self.run(_close(), timeout=60)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=5)
