"""Browser layer: config rules, the VM helper, the remote (SSH + tunnel) path and search fallback.

Everything runs on this machine with Playwright's Chromium. The remote path
uses tests/fake_ssh.py in place of ssh, so the helper, the tunnel and the CDP
attach are exercised exactly as against the VM, minus the network.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import sync_playwright

from scout.browser import open_browser
from scout.browser.config import ConfigError, load_browser_config, parse_browser_config
from scout.browser.hosts import helper_start_args
from scout.tools.search import DuckDuckGo, normalize_url, search_with

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "search"
HELPER = ROOT / "deploy" / "browser-vm" / "scout-browser"


@pytest.fixture(scope="module")
def chromium_path() -> str:
    with sync_playwright() as p:
        return p.chromium.executable_path


def run(coro):
    return asyncio.run(coro)


# -- config ------------------------------------------------------------------

def test_shipped_config_loads():
    cfg = load_browser_config()
    research = cfg.profile("research")
    assert research.host.kind == "ssh" and research.browser == "brave" and research.port == 9301
    assert research.timezone                                 # inherited from defaults (SCOUT_TIMEZONE or UTC)
    assert cfg.profile("research-tor").timezone == "UTC"
    assert "${" not in research.host.address and "${" not in research.host.live_view
    opts = research.host.ssh_opts                          # Scout's own key, ~ expanded
    assert opts[0] == "-i" and opts[1].endswith("/.ssh/scout_ed25519") and "~" not in opts[1]
    assert cfg.profile("local-test").host.ssh_opts == []
    assert cfg.search.engines == ("duckduckgo", "brave", "google")
    assert cfg.profile("local-test").headless


@pytest.mark.parametrize("raw, message", [
    ({"hosts": {"h": {"kind": "ssh"}}}, "needs an address"),
    ({"hosts": {"h": {"kind": "local"}}, "profiles": {"p": {"host": "nope"}}}, "unknown host"),
    ({"hosts": {"h": {"kind": "local"}}, "profiles": {"p": {"host": "h", "browser": "opera"}}}, "browser must be"),
    ({"hosts": {"h": {"kind": "local"}},
      "profiles": {"a": {"host": "h", "port": 9301}, "b": {"host": "h", "port": 9301}}}, "both use port"),
    ({"hosts": {"h": {"kind": "local"}}, "profiles": {"p": {"host": "h"}},
      "search": {"engines": ["bing"]}}, "unknown engines"),
])
def test_config_rejects_mistakes(raw, message):
    with pytest.raises(ConfigError, match=message):
        parse_browser_config(raw)


def test_env_values_fill_the_config(monkeypatch):
    from scout.browser.config import expand_env
    monkeypatch.setenv("SCOUT_VM_HOST", "vm-1")
    monkeypatch.delenv("SCOUT_TAILNET", raising=False)
    raw = expand_env({"a": "https://${SCOUT_VM_HOST:-x}.${SCOUT_TAILNET:-example.ts.net}/", "b": ["${SCOUT_NOPE}"], "c": 3})
    assert raw == {"a": "https://vm-1.example.ts.net/", "b": [""], "c": 3}
    with pytest.raises(ConfigError, match="needs an address"):
        parse_browser_config(expand_env({"hosts": {"h": {"kind": "ssh", "address": "${SCOUT_NOPE}"}}}))


def test_helper_args_carry_profile_settings():
    cfg = parse_browser_config({
        "hosts": {"vm": {"kind": "ssh", "address": "vm", "user": "scout"}},
        "profiles": {"jobs": {"host": "vm", "port": 9302, "browser": "brave", "locale": "de-DE",
                              "proxy": "socks5://10.0.0.5:1080", "extensions": ["/ext/ublock"],
                              "flags": ["--disable-gpu"], "window": [1440, 900]}}})
    args = helper_start_args(cfg.profile("jobs"))
    assert args[:2] == ["start", "jobs"]
    for expected in (["--port", "9302"], ["--locale", "de-DE"], ["--window", "1440,900"],
                     ["--proxy", "socks5://10.0.0.5:1080"], ["--extension", "/ext/ublock"]):
        i = args.index(expected[0])
        assert args[i:i + 2] == expected
    assert "--flag=--disable-gpu" in args


# -- url handling --------------------------------------------------------------

def test_normalize_url_dedupes_variants():
    a = normalize_url("https://www.Example.org/report/?utm_source=x&id=3#top")
    b = normalize_url("https://example.org/report?id=3")
    assert a == b


def test_duckduckgo_unwraps_redirects():
    wrapped = "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fa%3Fb%3D1&rut=x"
    assert DuckDuckGo._unwrap(wrapped) == "https://example.org/a?b=1"


# -- local host ----------------------------------------------------------------

def test_local_profile_attaches_and_stops():
    async def go():
        async with open_browser("local-test") as b:
            page = await b.new_page()
            await page.set_content("<title>ok</title>")
            assert await page.title() == "ok"
            url = b.endpoint.http_url
        return url
    url = run(go())
    with pytest.raises(httpx.HTTPError):
        httpx.get(url + "/json/version", timeout=1)        # stop_on_exit: the browser is gone


# -- VM helper, run locally ------------------------------------------------------

def _helper(home: Path, *args: str) -> dict:
    r = subprocess.run([sys.executable, str(HELPER), *args], capture_output=True, text=True, timeout=60,
                       env=dict(os.environ, SCOUT_BROWSER_HOME=str(home)))
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_helper_start_status_stop(tmp_path, chromium_path):
    port = str(_free_port())
    started = _helper(tmp_path, "start", "smoke", "--port", port, "--browser", chromium_path, "--headless")
    try:
        assert started["ok"] and started["ready"] and not started["already_running"]
        again = _helper(tmp_path, "start", "smoke", "--port", port, "--browser", chromium_path, "--headless")
        assert again["already_running"]
        status = _helper(tmp_path, "status")
        assert status["profiles"]["smoke"]["port"] == int(port)
        assert _helper(tmp_path, "start", "Bad Name", "--port", port)["ok"] is False
    finally:
        stopped = _helper(tmp_path, "stop", "smoke")
    assert stopped["was_running"]
    assert (tmp_path / "profiles" / "smoke").is_dir()         # profile data is kept for next time


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# -- remote path: helper over "ssh" + tunnel + CDP attach -------------------------------

@pytest.fixture
def fake_ssh_env(tmp_path, monkeypatch, chromium_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "ssh").symlink_to(ROOT / "tests" / "fake_ssh.py")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("SCOUT_BROWSER_HOME", str(tmp_path / "vm-home"))
    port = _free_port()
    # Stands in for /usr/local/bin/scout-browser on the VM.
    wrapper = bindir / "scout-browser"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {HELPER} \"$@\"\n")
    wrapper.chmod(0o755)
    raw = {"hosts": {"vm": {"kind": "ssh", "address": "vm", "user": "scout", "helper": str(wrapper),
                            "live_view": "https://vm.example/vnc.html"}},
           "profiles": {"remote": {"host": "vm", "port": port, "browser": "chromium", "headless": True}},
           "search": {"profile": "remote"}}
    # The helper looks binaries up by name; point "chromium" at Playwright's build.
    # (A wrapper, not a symlink: macOS Chromium aborts when started outside its app bundle.)
    shim = bindir / "chromium"
    shim.write_text(f'#!/bin/sh\nexec "{chromium_path}" "$@"\n')
    shim.chmod(0o755)
    return parse_browser_config(raw)


def test_remote_profile_through_helper_and_tunnel(fake_ssh_env):
    cfg = fake_ssh_env

    async def go():
        async with open_browser("remote", cfg) as b:
            assert b.endpoint.info["host"] == "vm"
            assert b.endpoint.info["local_port"] != cfg.profile("remote").port   # really tunnelled
            page = await b.new_page()
            await page.set_content("<title>remote</title>")
            assert await page.title() == "remote"
            assert b.live_view == "https://vm.example/vnc.html"
    run(go())
    # stop_on_exit is false: the remote browser keeps running for the next task.
    port = cfg.profile("remote").port
    assert httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=2).status_code == 200
    subprocess.run([str(Path(os.environ["PATH"].split(os.pathsep)[0]) / "scout-browser"), "stop", "remote"],
                   capture_output=True, timeout=30)


# -- search fallback, engines served from fixtures --------------------------------------

def _fixture(name: str, query: str, page: int) -> str:
    return (FIXTURES / name).read_text().replace("{query}", query).replace("{page}", str(page))


async def _search_with_fixtures(pages: dict[str, str], engines=("duckduckgo", "brave", "google"), want=10,
                                max_pages=2):
    cfg = load_browser_config()
    settings = dataclasses.replace(cfg.search, engines=tuple(engines), results_wanted=want,
                                   max_pages_per_engine=max_pages, pause_seconds=(0, 0))
    async with open_browser("local-test", cfg) as b:
        async def handle(route):
            url = route.request.url
            host = "duckduckgo" if "duckduckgo" in url else "brave" if "brave.com" in url else "google"
            page_no = 2 if any(k in url for k in ("&s=", "offset=1", "start=10")) else 1
            await route.fulfill(status=200, content_type="text/html",
                                body=_fixture(pages[host], "q", page_no))
        await b.context.route("**/*", handle)
        try:
            return await search_with(b, "hydrogen storage", settings, want=want)
        finally:
            await b.context.unroute("**/*")


def test_search_pages_engine_then_falls_back():
    out = run(_search_with_fixtures({"duckduckgo": "duckduckgo_results.html", "brave": "brave_results.html",
                                     "google": "google_results.html"}, want=4))
    urls = [r.url for r in out.results]
    assert "https://ads.example.com/buy" not in urls                      # ads dropped
    assert urls[0] == "https://example.org/hydrogen-blending?utm_source=ddg"  # redirect unwrapped
    assert [a.engine for a in out.attempts] == ["duckduckgo", "brave"]   # Google never needed
    ddg, brave = out.attempts
    assert ddg.pages == 2 and ddg.results == 3                           # page 2 added one new result
    assert brave.results == 1                                            # duplicate of DDG skipped
    assert [r.rank for r in out.results] == [1, 2, 3, 4]
    assert not out.needs_human


def test_search_skips_blocked_engines():
    out = run(_search_with_fixtures({"duckduckgo": "duckduckgo_blocked.html", "brave": "brave_blocked.html",
                                     "google": "google_results.html"}, want=3))
    blocked = {a.engine: a.blocked for a in out.attempts}
    assert blocked["duckduckgo"] and blocked["brave"] and not blocked["google"]
    assert out.results and all(r.engine == "google" for r in out.results)
    assert all("google.com" not in r.url for r in out.results)


def test_search_all_blocked_asks_for_a_person():
    out = run(_search_with_fixtures({"duckduckgo": "duckduckgo_blocked.html", "brave": "brave_blocked.html",
                                     "google": "google_blocked.html"}))
    assert out.results == []
    assert all(a.blocked for a in out.attempts)
    assert out.needs_human


# -- real result pages captured on the browser VM (scripts/browser_check.py --capture) ----

async def _parse_capture(engine_name: str, monkeypatch=None) -> list[dict]:
    from scout.tools.search import ENGINES
    html = (FIXTURES / f"{engine_name}_live.html").read_text()
    engine = ENGINES[engine_name]
    async with open_browser("local-test") as b:
        page = await b.new_page()

        async def handle(route):
            if route.request.resource_type == "document":
                await route.fulfill(status=200, content_type="text/html", body=html)
            else:
                await route.abort()
        await page.route("**/*", handle)
        await page.goto(engine.url("hydrogen storage in natural gas pipelines", 0))
        assert await engine.blocked(page) is None
        return await engine.extract(page)


@pytest.mark.parametrize("engine_name, at_least", [("duckduckgo", 10), ("brave", 10)])
def test_live_capture_parses(engine_name, at_least):
    rows = run(_parse_capture(engine_name))
    assert len(rows) >= at_least
    for r in rows:
        assert r["url"].startswith("http") and r["title"]
        host = r["url"].split("/")[2]
        assert not host.endswith(("duckduckgo.com", "brave.com"))
    assert sum(bool(r["snippet"]) for r in rows) >= len(rows) // 2


def test_live_google_capture_resolves_redirect_links(monkeypatch):
    from scout.tools.search import Google
    seen = []

    async def fake_resolve(page, url):          # the real one follows Google's 302; no network here
        seen.append(url)
        return f"https://example.org/result-{len(seen)}"
    monkeypatch.setattr(Google, "_resolve", staticmethod(fake_resolve))
    rows = run(_parse_capture("google"))
    assert len(rows) >= 8 and all("/goto?url=" in u for u in seen)
    assert all(r["title"] for r in rows)


def test_google_readable_redirect_unwrapped():
    from scout.tools.search import Google
    url = "https://www.google.com/url?q=https://example.org/a%3Fb%3D1&sa=U"
    assert run(Google._resolve(None, url)) == "https://example.org/a?b=1"
    assert run(Google._resolve(None, "https://example.org/x")) == "https://example.org/x"
    assert run(Google._resolve(None, "https://www.google.com/search?q=x")) is None


# -- Tor on/off ----------------------------------------------------------------------

def _tor_cfg(mode: str):
    return parse_browser_config({
        "hosts": {"h": {"kind": "local"}},
        "profiles": {"direct": {"host": "h", "port": 9301},
                     "tor": {"host": "h", "port": 9302, "proxy": "socks5://127.0.0.1:9050"}},
        "search": {"profile": "direct", "tor_profile": "tor", "tor": mode}})


@pytest.mark.parametrize("mode, answer, expected", [
    ("on", None, ("tor", True)), ("off", None, ("direct", False)),
    ("ask", True, ("tor", True)), ("ask", False, ("direct", False)),
    ("off", True, ("tor", True)),                     # the user's per-call answer beats the default
])
def test_tor_choice(mode, answer, expected):
    from scout.tools.search import resolve_tor
    assert resolve_tor(_tor_cfg(mode), answer) == expected


def test_tor_ask_mode_refuses_to_search_unasked():
    from scout.tools.search import TorChoiceRequired, resolve_tor
    with pytest.raises(TorChoiceRequired, match="ask the user"):
        resolve_tor(_tor_cfg("ask"), None)


def test_tor_mode_is_validated():
    with pytest.raises(ConfigError, match="tor must be one of"):
        _tor_cfg("maybe")


def test_web_search_asks_before_searching(monkeypatch):
    import scout.tools.search as search
    monkeypatch.setattr(search, "load_browser_config", lambda: _tor_cfg("ask"))
    opened = []

    class FakeSession:
        live_view = None

    class FakeOpen:
        def __init__(self, profile, cfg): opened.append(profile)
        async def __aenter__(self): return FakeSession()
        async def __aexit__(self, *exc): return False

    async def fake_search_with(session, query, settings, want=None):
        return search.SearchOutcome(query=query, results=[], attempts=[])
    monkeypatch.setattr(search, "open_browser", FakeOpen)
    monkeypatch.setattr(search, "search_with", fake_search_with)

    asked = []
    async def ask():
        asked.append(True)
        return True
    out = run(search.web_search("q", ask=ask))
    assert asked == [True] and opened == ["tor"] and out.tor is True
    with pytest.raises(search.TorChoiceRequired):
        run(search.web_search("q"))                   # ask mode, nobody to ask: no search at all
    assert opened == ["tor"]


# -- committed captures carry no personal or session data ------------------------------

@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.html")), ids=lambda p: p.name)
def test_fixtures_are_scrubbed(path):
    from scout.tools.scrub import scrub_html
    html = path.read_text()
    assert scrub_html(html) == html, f"{path.name} was not scrubbed: run scout.tools.scrub.scrub_html on it"


def test_scrub_removes_location_and_tokens(monkeypatch):
    from scout.tools.scrub import scrub_html
    monkeypatch.setenv("SCOUT_SCRUB_PLACES", "Springfield, Freedonia")
    monkeypatch.setenv("SCOUT_SCRUB_REGIONS", "ZZ")
    raw = ('<html lang="en-ZZ"><script>geoLocation:"12.345x67.891"</script><a href="/goto?url=AbC&gsessionid=xyz">t</a>'
           '<a href="/goto?url=DeF&gl=zz">u</a><p>Results near Springfield, Freedonia from 203.0.113.9</p>'
           '<input type="hidden" name="sxsrf" value="tok"><div data-ved="2ah">x</div>'
           '<svg><path d="M712.31-140q-45.23,0-76.27-31.04"/></svg>'
           '<a href="https://www.google.zz/intl/en/about">a</a><i data-x="[&quot;en&quot;,&quot;ZZ&quot;]"></i>')
    out = scrub_html(raw)
    for leaked in ("12.345", "AbC", "xyz", "Springfield", "Freedonia", "203.0.113.9", "tok", "2ah", "en-ZZ", "gl=zz", "google.zz", "&quot;ZZ&quot;"):
        assert leaked not in out
    assert "M712.31-140q-45.23,0-76.27-31.04" in out          # SVG path data untouched
    assert "/goto?url=REDACTED-1" in out and "/goto?url=REDACTED-2" in out
