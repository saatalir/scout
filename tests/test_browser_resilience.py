"""B7 (written by Strata through the delegation harness, reviewed): misbehaving fixture pages against the browser
runner: slow and flaky pages, late and re-rendered content, overlays, infinite scroll, popups, pushState, banners."""

from __future__ import annotations

import re
import time

import pytest

from browser_fixtures import BASE, make_routes
from scout.browser.agent_tools import BrowserRunner


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOUT_LIBRARY_DIR", str(tmp_path / "library"))
    monkeypatch.setenv("SCOUT_TASKS_DIR", str(tmp_path / "tasks"))


@pytest.fixture
def runner():
    r = BrowserRunner("local-test", setup=make_routes())
    try:
        yield r
    finally:
        r.close()


def eid(out: dict, needle: str) -> str:
    for line in out.get("elements", []):
        m = re.match(r'^(e\d+) .*?"(.*?)"', line)
        if m and m.group(2) == needle:
            return m.group(1)
    for line in out.get("elements", []):
        if needle in line:
            return line.split()[0]
    raise AssertionError(f"no element containing {needle!r} in {out.get('elements')}")


def has(out: dict, needle: str) -> bool:
    return any(needle in line for line in out.get("elements", []))


def line_for(out: dict, needle: str) -> str:
    for line in out.get("elements", []):
        if needle in line:
            return line
    raise AssertionError(f"no element line containing {needle!r} in {out.get('elements')}")


def open_page(runner: BrowserRunner, path: str) -> dict:
    return runner.call("browser_open", {"url": f"{BASE}{path}"}, approve=lambda s: False)


def test_slow_page(runner):
    out = open_page(runner, "/slow?delay=3")
    assert "error" not in out
    assert "Finally loaded." in out["text"]


def test_flaky_navigation(runner):
    out = open_page(runner, "/slow?flaky=1")
    assert "error" not in out
    assert out["title"] == "Slow page"


def test_permanently_failing_navigation(runner):
    out = open_page(runner, "/slow?flaky=10")
    assert "error" in out
    assert out["error"].startswith("could not open")


def test_http_error_page(runner):
    out = open_page(runner, "/error?status=500")
    assert out["http_status"] == 500
    assert "Something went wrong" in out["text"]


def test_late_content(runner):
    out = open_page(runner, "/late_content")
    assert not has(out, "Late button")
    first_id = eid(out, "First")

    time.sleep(1.5)
    out2 = runner.call("browser_read", {}, approve=lambda s: False)

    assert has(out2, "Late button")
    assert eid(out2, "First") == first_id


def test_rerender_invalidates_ids(runner):
    out = open_page(runner, "/rerender")
    old_item1 = eid(out, "Item 1")
    shuffle = eid(out, "Shuffle")

    out2 = runner.call("browser_click", {"element_id": shuffle}, approve=lambda s: False)
    assert has(out2, "Item A")

    err = runner.call("browser_click", {"element_id": old_item1}, approve=lambda s: False)
    assert "error" in err
    assert "browser_read" in err["error"]


def test_unknown_id(runner):
    open_page(runner, "/disabled")
    err = runner.call("browser_click", {"element_id": "e999"}, approve=lambda s: False)
    assert "error" in err
    assert "browser_read" in err["error"]


@pytest.mark.slow
def test_covered_element(runner):
    out = open_page(runner, "/overlay")
    hidden = eid(out, "Hidden behind")

    err = runner.call("browser_click", {"element_id": hidden}, approve=lambda s: False)
    assert "error" in err
    assert "did not respond in time" in err["error"]

    out2 = runner.call("browser_read", {}, approve=lambda s: False)
    remove = eid(out2, "Remove overlay")

    removed = runner.call(
        "click_submit",
        {"element_id": remove, "action_summary": "Remove the overlay"},
        approve=lambda s: True,
    )
    assert "error" not in removed

    hidden = eid(removed, "Hidden behind")
    ok = runner.call("browser_click", {"element_id": hidden}, approve=lambda s: False)
    assert "error" not in ok


def test_infinite_scroll(runner):
    out = open_page(runner, "/infinite")
    assert not has(out, "Story 15")

    for _ in range(4):
        out = runner.call("browser_scroll", {"direction": "down"}, approve=lambda s: False)
        if has(out, "Story 15"):
            break

    assert has(out, "Story 15")


def test_window_open_popup_is_followed(runner):
    out = open_page(runner, "/opener")
    button = eid(out, "Open window")

    out2 = runner.call("browser_click", {"element_id": button}, approve=lambda s: False)
    assert "note" in out2
    assert "new tab or popup" in out2["note"]
    assert out2["title"] == "Child"

    out3 = runner.call("browser_back", {}, approve=lambda s: False)
    assert "note" in out3
    assert "Closed" in out3["note"]
    assert out3["title"] == "Opener"


def test_target_blank_tab_is_followed(runner):
    out = open_page(runner, "/opener")
    link = eid(out, "Child in a tab")

    out2 = runner.call("browser_click", {"element_id": link}, approve=lambda s: False)
    assert "note" in out2
    assert "new tab or popup" in out2["note"]
    assert out2["title"] == "Child"

    out3 = runner.call("browser_back", {}, approve=lambda s: False)
    assert "note" in out3
    assert "Closed" in out3["note"]
    assert out3["title"] == "Opener"


def test_pushstate(runner):
    out = open_page(runner, "/pushstate")
    button = eid(out, "Next step")

    out2 = runner.call("browser_click", {"element_id": button}, approve=lambda s: False)
    assert out2["url"].endswith("step=2")
    assert "Step 2" in out2["text"]


def test_late_cookie_banner(runner):
    out = open_page(runner, "/late_banner")
    assert "cookie_banner" not in out

    time.sleep(1.2)
    out2 = runner.call("browser_read", {}, approve=lambda s: False)
    assert "cookie_banner" in out2 or "consent" in out2        # flagged, or declined by Scout (B6)


def test_disabled_element(runner):
    out = open_page(runner, "/disabled")
    assert "disabled" in line_for(out, "Not yet")


def test_browser_back_with_no_history(runner):
    out = runner.call("browser_back", {}, approve=lambda s: False)
    assert isinstance(out, dict)
    if "error" in out:
        assert out["error"] == "there is no previous page"
