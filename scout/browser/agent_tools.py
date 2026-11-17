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
the page" error rather than a crash.
"""

from __future__ import annotations

import asyncio
import re
import threading
from contextlib import AsyncExitStack
from typing import Any, Awaitable, Callable

from playwright.async_api import Error as PWError
from playwright.async_api import TimeoutError as PWTimeout

from .config import BrowserConfig, load_browser_config
from .page import PageView, locator_for, snapshot
from .session import open_browser

SUBMIT_WORDS = re.compile(
    r"\b(submit|send|pay|purchase|buy|order|checkout|check out|confirm|apply|sign up|register|delete|remove|"
    r"publish|post|book|reserve|subscribe|place order|transfer|accept terms)\b", re.I)
NAV_TIMEOUT_MS = 45000


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
        return self.page

    async def _read(self) -> dict:
        page = await self._ensure()
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10000)
        except PWTimeout:
            pass
        self.view = await snapshot(page)
        return self.view.for_model()

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
        return await self._read()

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
        if name == "click_submit":
            summary = str(a.get("action_summary") or f'Click "{el.name}" on {self.view.url}')
            if not approve(summary):
                return {"decision": "denied", "note": "The human did not approve; nothing was clicked."}
            out = self.run(self._act(eid, lambda loc: loc.click(timeout=15000)))
            if isinstance(out, dict) and "error" not in out:
                out["decision"] = "approved"
            return out
        raise ValueError(f"unknown browser tool {name!r}")

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
