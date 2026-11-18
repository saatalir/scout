"""B6 consent handling: platform detection, reject clicks, generic fallback, regex safety."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from scout.browser import open_browser
from scout.browser.consent import (
    ACCEPT_TEXT,
    REJECT_TEXT,
    is_reject_label,
    platform_names,
    reject_consent,
)

FIXTURES = Path(__file__).parent / "fixtures" / "consent"


def run(coro):
    return asyncio.run(coro)


async def with_page(fn, name):
    async with open_browser("local-test") as b:
        page = await b.new_page()

        async def handler(route):
            path = route.request.url.split("consent.test")[-1]
            if path == "/sp_message_iframe/index.html":
                path = "/sourcepoint_frame.html"
            file = FIXTURES / path.lstrip("/")
            if file.exists():
                await route.fulfill(body=file.read_text(), content_type="text/html")
            else:
                await route.abort()

        await b.context.route("https://consent.test/**", handler)
        await page.goto(f"https://consent.test/{name}.html")
        return await fn(page)


def test_platform_names():
    names = platform_names()
    assert "onetrust" in names
    assert "sourcepoint" in names


@pytest.mark.parametrize("name", [
    "onetrust", "cookiebot", "didomi", "quantcast", "usercentrics", "trustarc",
    "sourcepoint", "google_fc", "cookieyes", "complianz", "osano", "iubenda", "klaro"
])
def test_platform_pages(name):
    async def go(page):
        r = await reject_consent(page, timeout_ms=1000)
        consent = await page.evaluate("document.body.dataset.consent")
        return r, consent
    r, consent = run(with_page(go, name))
    assert r is not None and r["platform"] == name
    assert r["closed"] is True
    assert consent == "rejected"


def test_onetrust_settings():
    async def go(page):
        r = await reject_consent(page, timeout_ms=1000)
        consent = await page.evaluate("document.body.dataset.consent")
        return r, consent
    r, consent = run(with_page(go, "onetrust_settings"))
    assert r is not None and r["platform"] == "onetrust" and r["via_settings"] is True
    assert consent == "rejected"


@pytest.mark.parametrize("name", ["generic_de", "generic_el", "generic_en"])
def test_generic_pages(name):
    async def go(page):
        r = await reject_consent(page, timeout_ms=1000)
        consent = await page.evaluate("document.body.dataset.consent")
        return r, consent
    r, consent = run(with_page(go, name))
    assert r is not None and r["platform"] == "generic"
    assert consent == "rejected"


def test_accept_only_and_no_banner():
    async def go(page):
        r = await reject_consent(page, timeout_ms=500)
        consent = await page.evaluate("document.body.dataset.consent")
        return r, consent
    r1, c1 = run(with_page(go, "accept_only"))
    assert r1 is None and c1 is None
    r2, c2 = run(with_page(go, "no_banner"))
    assert r2 is None and c2 is None


def test_late_banner():
    async def go(page):
        r1 = await reject_consent(page, timeout_ms=500)
        await asyncio.sleep(1.2)
        r2 = await reject_consent(page, timeout_ms=1000)
        consent = await page.evaluate("document.body.dataset.consent")
        return r1, r2, consent
    r1, r2, consent = run(with_page(go, "late_banner"))
    assert r1 is None
    assert r2 is not None and r2["platform"] == "onetrust"
    assert consent == "rejected"


def test_regex_unit():
    assert REJECT_TEXT.search("απόρριψη όλων")
    assert REJECT_TEXT.search("alle ablehnen")
    assert ACCEPT_TEXT.search("Accept all")
    assert ACCEPT_TEXT.search("Αποδοχή όλων")
    assert is_reject_label("Accept only necessary")
    assert not is_reject_label("Accept all")


# -- Claude's review: accept labels are never clicked; Scout declines banners on its own ------------------------

@pytest.mark.parametrize("label", [
    "Accept non-essential cookies", "Accept all, including essential", "Allow essential and analytics",
    "I agree", "Got it", "OK", "Alle akzeptieren", "Accepter et continuer", "Αποδοχή όλων", "Accept or reject",
    "Necessary", "Cookie settings", "Learn more", "Reject all and subscribe", "SUBSCRIBE AND DECLINE",
    "Log in to refuse", "Refuser et s'abonner", "No, take me to settings"])
def test_accept_and_unclear_labels_are_never_reject_labels(label):
    assert not is_reject_label(label)


@pytest.mark.parametrize("label", [
    "Reject all", "Reject non-essential", "Decline", "Deny all", "Do not consent", "Continue without accepting",
    "Accept only necessary", "Use necessary cookies only", "Required only", "Alle ablehnen",
    "Nur notwendige Cookies", "Tout refuser", "Continuer sans accepter", "Rechazar todo", "Rifiuta tutto",
    "Απόρριψη όλων", "Μόνο τα απαραίτητα", "I do not agree", "Disagree"])
def test_reject_labels(label):
    assert is_reject_label(label)


def test_a_platform_selector_on_an_accept_button_is_not_clicked():
    page_html = """<html><body><main>x</main><div id="onetrust-banner-sdk" style="position:fixed;bottom:0">
      <button id="onetrust-reject-all-handler" onclick="document.body.dataset.consent='accepted'">Accept all</button>
      </div></body></html>"""

    async def go(page):
        await page.set_content(page_html)
        return await reject_consent(page, timeout_ms=500), await page.evaluate("document.body.dataset.consent")

    async def main():
        async with open_browser("local-test") as b:
            return await go(await b.new_page())
    r, consent = run(main())
    assert r is None and consent is None


def test_runner_declines_banners_itself_including_in_iframes(monkeypatch):
    from scout.browser.agent_tools import BrowserRunner
    monkeypatch.setenv("SCOUT_CONSENT_WAIT", "3")                     # the iframe may load after the page

    async def setup(context):
        async def handler(route):
            path = route.request.url.split("consent.test")[-1].split("?")[0]
            if path == "/sp_message_iframe/index.html":
                path = "/sourcepoint_frame.html"
            file = FIXTURES / path.lstrip("/")
            await (route.fulfill(body=file.read_text(), content_type="text/html") if file.exists() else route.abort())
        await context.route("https://consent.test/**", handler)
    r = BrowserRunner("local-test", setup=setup)
    try:
        for name, platform in (("generic_en", "generic"), ("sourcepoint", "sourcepoint"), ("usercentrics", "usercentrics"),
                               ("late_banner", "onetrust"), ("sourcepoint_by_id", "sourcepoint")):
            out = r.call("browser_open", {"url": f"https://consent.test/{name}.html"}, approve=lambda s: False)
            assert out.get("consent", "").startswith(f"Declined non-essential cookies ({platform}"), (name, out)
        out = r.call("browser_open", {"url": "https://consent.test/accept_only.html"}, approve=lambda s: False)
        assert "consent" not in out and "Do not accept it" in out["cookie_banner"]   # nothing safe: left, explained
    finally:
        r.close()
