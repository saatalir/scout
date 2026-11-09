"""Web search through a real browser: no API keys.

Engines are tried in the configured order (DuckDuckGo, Brave Search, Google
by default). Each engine is paged through until enough results are found; the
next engine is used only when the earlier ones were blocked or came up short.

Scout never tries to get past a CAPTCHA or bot check. A blocked engine is
recorded and skipped; if every engine is blocked, the outcome says a person is
needed and carries the live-view link where they can solve it.
"""

from __future__ import annotations

import asyncio
import random
import re
from dataclasses import asdict, dataclass, field
from typing import Awaitable, Callable, Protocol
from urllib.parse import parse_qs, quote_plus, urlencode, urlparse, urlunparse

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PWTimeout

from ..browser import BrowserConfig, BrowserSession, SearchConfig, load_browser_config, open_browser

TRACKING = re.compile(r"^(utm_|fbclid$|gclid$|mc_|ref$|ref_src$)")


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    engine: str
    rank: int          # 1-based position across the merged list


@dataclass
class EngineAttempt:
    engine: str
    pages: int = 0
    results: int = 0   # new results this engine added
    blocked: str | None = None
    error: str | None = None


@dataclass
class SearchOutcome:
    query: str
    results: list[SearchResult]
    attempts: list[EngineAttempt]
    needs_human: bool = False
    live_view: str | None = None
    tor: bool = False          # True when the search went through the Tor profile

    def to_dict(self) -> dict:
        return asdict(self)


def normalize_url(url: str) -> str:
    """Key for de-duplication: no fragment, tracking params or trailing slash; host lower-cased."""
    p = urlparse(url)
    query = [(k, v) for k, vs in parse_qs(p.query, keep_blank_values=True).items()
             for v in vs if not TRACKING.match(k)]
    host = p.netloc.lower().removeprefix("www.")
    return urlunparse((p.scheme.lower(), host, p.path.rstrip("/") or "/", "", urlencode(query), ""))


# -- engines -----------------------------------------------------------------

class Engine(Protocol):
    name: str

    def url(self, query: str, page_index: int) -> str: ...
    async def prepare(self, page: Page) -> None: ...
    async def blocked(self, page: Page) -> str | None: ...
    async def extract(self, page: Page) -> list[dict]: ...


BLOCK_TEXT = re.compile(
    r"unusual traffic|are you a robot|verify you are (a )?human|bots use duckduckgo too|"
    r"captcha|confirm this search was made by a human|press and hold|flagged as being suspicious", re.I)


async def _body_text(page: Page, limit: int = 4000) -> str:
    try:
        return (await page.inner_text("body", timeout=3000))[:limit]
    except PWTimeout:
        return ""


async def looks_blocked(page: Page, results_selector: str) -> bool:
    """A bot check: a challenge title, or challenge wording on a page with no results."""
    try:
        title = await page.title()
    except Exception:  # noqa: BLE001
        title = ""
    if re.search(r"captcha|sorry|are you a robot|verify", title, re.I):
        return True
    if await page.locator(results_selector).count():
        return False
    return bool(BLOCK_TEXT.search(await _body_text(page)))


class DuckDuckGo:
    """The no-JavaScript HTML endpoint: stable markup, rarely blocked."""
    name = "duckduckgo"

    def url(self, query: str, page_index: int) -> str:
        # The HTML endpoint pages with an offset `s` (30 results a page after the first).
        q = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        return q if page_index == 0 else f"{q}&s={30 * page_index - 5}&dc={30 * page_index - 4}"

    async def prepare(self, page: Page) -> None:
        return None

    async def blocked(self, page: Page) -> str | None:
        if await page.locator("#challenge-form, .anomaly-modal__mask").count():
            return "bot challenge"
        return "bot challenge" if await looks_blocked(page, ".result, .no-results") else None

    async def extract(self, page: Page) -> list[dict]:
        rows = await page.evaluate("""() => [...document.querySelectorAll('.result')]
            .filter(r => !r.classList.contains('result--ad'))
            .map(r => {
              const a = r.querySelector('a.result__a');
              const s = r.querySelector('.result__snippet');
              return a ? {title: a.innerText.trim(), url: a.href, snippet: s ? s.innerText.trim() : ''} : null;
            }).filter(Boolean)""")
        for r in rows:
            r["url"] = self._unwrap(r["url"])
        return rows

    @staticmethod
    def _unwrap(url: str) -> str:
        """DuckDuckGo wraps result links in /l/?uddg=<target>."""
        p = urlparse(url)
        if p.netloc.endswith("duckduckgo.com") and p.path.startswith("/l/"):
            target = parse_qs(p.query).get("uddg")
            if target:
                return target[0]
        return url


class BraveSearch:
    name = "brave"

    def url(self, query: str, page_index: int) -> str:
        return f"https://search.brave.com/search?q={quote_plus(query)}&source=web&offset={page_index}"

    async def prepare(self, page: Page) -> None:
        return None

    async def blocked(self, page: Page) -> str | None:
        if "/captcha" in page.url:
            return "captcha"
        return "captcha" if await looks_blocked(page, self.RESULTS) else None

    # Result cards carry data-type="web"; the page around them has moved before
    # (#results, then main #mixed-main), so match the cards wherever they sit.
    RESULTS = 'main [data-type="web"], #results [data-type="web"]'

    async def extract(self, page: Page) -> list[dict]:
        return await page.evaluate("""(sel) => [...document.querySelectorAll(sel)]
            .map(r => {
              const a = [...r.querySelectorAll('a[href^="http"]')].find(x => !new URL(x.href).hostname.endsWith('brave.com'));
              if (!a) return null;
              const t = r.querySelector('.title, [class*="title"]');
              const d = r.querySelector('.snippet-description, [class*="description"], .content');
              return {title: ((t || a).innerText || '').trim().split('\\n')[0], url: a.href,
                      snippet: d ? d.innerText.trim() : ''};
            }).filter(Boolean)""", self.RESULTS)


class Google:
    name = "google"

    def url(self, query: str, page_index: int) -> str:
        return f"https://www.google.com/search?q={quote_plus(query)}&hl=en&num=10&start={10 * page_index}"

    async def prepare(self, page: Page) -> None:
        # EU visitors get a cookie consent first, either on consent.google.com or
        # as a dialog over the results page. Decline everything non-essential.
        if "consent." in urlparse(page.url).netloc or await page.locator("form[action*='consent.google']").count() \
                or await page.get_by_role("button", name=re.compile(r"^reject all$", re.I)).count():
            button = page.get_by_role("button", name=re.compile(r"^reject all$", re.I))
            if await button.count():
                await button.first.click()
                await page.wait_for_load_state("domcontentloaded")
                try:
                    await page.locator("#search a:has(h3), #rso a:has(h3)").first.wait_for(timeout=10000)
                except PWTimeout:
                    pass

    async def blocked(self, page: Page) -> str | None:
        if "/sorry/" in page.url or await page.locator("form#captcha-form, #recaptcha").count():
            return "captcha"
        return "captcha" if await looks_blocked(page, "#search a:has(h3)") else None

    async def extract(self, page: Page) -> list[dict]:
        rows = await page.evaluate("""() => {
            const seen = new Set();
            return [...document.querySelectorAll('#search a:has(h3), #rso a:has(h3)')].map(a => {
              if (!a.href.startsWith('http') || seen.has(a.href)) return null;
              seen.add(a.href);
              const box = a.closest('[data-hveid]') || a.parentElement;
              const sn = box ? [...box.querySelectorAll('[data-sncf], .VwiC3b, [style*="-webkit-line-clamp"]')]
                                 .map(e => e.innerText.trim()).filter(Boolean).join(' ') : '';
              return {title: a.querySelector('h3').innerText.trim(), url: a.href, snippet: sn};
            }).filter(Boolean);
        }""")
        out = []
        for r in rows:
            url = await self._resolve(page, r["url"])
            if url and not urlparse(url).netloc.endswith("google.com"):
                out.append({**r, "url": url})
        return out

    @staticmethod
    async def _resolve(page: Page, url: str) -> str | None:
        """Google wraps result links: /url?q=<target> (readable) or /goto?url=<opaque> (a redirect)."""
        p = urlparse(url)
        if not p.netloc.endswith("google.com"):
            return url
        if p.path == "/url":
            target = parse_qs(p.query).get("q") or parse_qs(p.query).get("url")
            return target[0] if target else None
        if p.path == "/goto":
            try:
                r = await page.context.request.get(url, max_redirects=0, timeout=15000)
            except Exception:  # noqa: BLE001 - an unresolvable link is just skipped
                return None
            return r.headers.get("location") or None
        return None


ENGINES: dict[str, Engine] = {e.name: e for e in (DuckDuckGo(), BraveSearch(), Google())}


# -- search ------------------------------------------------------------------

async def search_with(session: BrowserSession, query: str, settings: SearchConfig,
                      want: int | None = None, nav_timeout_ms: int = 30000) -> SearchOutcome:
    want = want or settings.results_wanted
    results: list[SearchResult] = []
    seen: set[str] = set()
    attempts: list[EngineAttempt] = []
    page = await session.new_page()
    try:
        for name in settings.engines:
            if len(results) >= want:
                break
            engine = ENGINES[name]
            att = EngineAttempt(engine=name)
            attempts.append(att)
            for i in range(settings.max_pages_per_engine):
                if i:
                    await asyncio.sleep(random.uniform(*settings.pause_seconds))
                try:
                    await page.goto(engine.url(query, i), wait_until="domcontentloaded", timeout=nav_timeout_ms)
                    await engine.prepare(page)
                    att.blocked = await engine.blocked(page)
                    if att.blocked:
                        break
                    rows = await engine.extract(page)
                except Exception as e:  # noqa: BLE001 - one engine failing must not end the search
                    att.error = f"{type(e).__name__}: {str(e).splitlines()[0][:200]}"
                    break
                att.pages += 1
                added = 0
                for row in rows:
                    key = normalize_url(row["url"])
                    if key in seen:
                        continue
                    seen.add(key)
                    added += 1
                    results.append(SearchResult(title=row["title"], url=row["url"], snippet=row["snippet"],
                                                engine=name, rank=len(results) + 1))
                att.results += added
                if added == 0 or len(results) >= want:
                    break
    finally:
        if not page.is_closed():
            await page.close()

    all_blocked = bool(attempts) and all(a.blocked for a in attempts)
    return SearchOutcome(query=query, results=results[:want], attempts=attempts,
                         needs_human=all_blocked and not results, live_view=session.live_view)


# -- Tor on/off ----------------------------------------------------------------

class TorChoiceRequired(RuntimeError):
    """Tor mode is "ask" and nobody has answered yet.

    The caller (an agent, the CLI) must ask the user "search through Tor: on or
    off?" before the first search of a task, then pass tor=True/False.
    """


AskTor = Callable[[], Awaitable[bool]]


def resolve_tor(cfg: BrowserConfig, tor: bool | None) -> tuple[str, bool]:
    """Pick the search profile for a Tor choice; returns (profile name, tor).

    tor=True/False is the user's answer and always wins. tor=None falls back to
    the configured mode (SCOUT_TOR): "on"/"off" decide, "ask" raises
    TorChoiceRequired so that the user is asked first.
    """
    if tor is None:
        mode = cfg.search.tor
        if mode == "ask":
            raise TorChoiceRequired(
                "Tor mode is 'ask': ask the user whether to search through Tor (on/off) "
                "before searching, then pass tor=True or tor=False.")
        tor = mode == "on"
    if tor and not cfg.search.tor_profile:
        raise TorChoiceRequired("Tor was requested but config/browsers.yaml sets no search.tor_profile.")
    return (cfg.search.tor_profile if tor else cfg.search.profile), bool(tor)


async def web_search(query: str, want: int | None = None, *, tor: bool | None = None,
                     ask: AskTor | None = None, profile: str | None = None) -> SearchOutcome:
    """Open the search profile, run one query, close Scout's pages.

    tor:     True = through Tor, False = direct, None = use SCOUT_TOR.
    ask:     called when SCOUT_TOR=ask and tor is None; returns the user's answer.
             Without it, that case raises TorChoiceRequired.
    profile: an explicit profile overrides the Tor choice entirely.
    """
    cfg = load_browser_config()
    if profile:
        used_tor = bool(cfg.profile(profile).proxy)
    else:
        try:
            profile, used_tor = resolve_tor(cfg, tor)
        except TorChoiceRequired:
            if ask is None:
                raise
            profile, used_tor = resolve_tor(cfg, await ask())
    async with open_browser(profile, cfg) as session:
        out = await search_with(session, query, cfg.search, want)
    out.tor = used_tor
    return out
