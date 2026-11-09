"""Open a browser profile and drive it with Playwright.

    async with open_browser("research") as b:
        page = await b.new_page()
        await page.goto("https://example.org")

Scout attaches over CDP to the profile's real browser and works in its
default context, so the profile's cookies and sign-ins apply. Closing the
session closes only the pages Scout opened; the browser keeps running unless
the profile says `stop_on_exit`.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from .config import BrowserConfig, Profile, load_browser_config
from .hosts import Endpoint, start_profile


@dataclass
class BrowserSession:
    profile: Profile
    endpoint: Endpoint
    browser: Browser
    context: BrowserContext
    _pages: list[Page] = field(default_factory=list, repr=False)

    @property
    def live_view(self) -> str | None:
        """Where a person can watch or take over (sign-ins, CAPTCHAs)."""
        return self.profile.host.live_view

    async def new_page(self) -> Page:
        page = await self.context.new_page()
        self._pages.append(page)
        return page

    async def close_pages(self) -> None:
        for page in self._pages:
            if not page.is_closed():
                await page.close()
        self._pages.clear()


@asynccontextmanager
async def open_browser(profile: str | Profile, config: BrowserConfig | None = None) -> AsyncIterator[BrowserSession]:
    if isinstance(profile, str):
        profile = (config or load_browser_config()).profile(profile)
    async with async_playwright() as pw:
        endpoint = await start_profile(profile, chromium_path=pw.chromium.executable_path)
        try:
            browser = await pw.chromium.connect_over_cdp(endpoint.ws_url)
            # A persistent profile has one default context holding its cookies.
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            session = BrowserSession(profile=profile, endpoint=endpoint, browser=browser, context=context)
            try:
                yield session
            finally:
                await session.close_pages()
                await browser.close()   # over CDP this disconnects; it does not quit the browser
        finally:
            await endpoint.release()
