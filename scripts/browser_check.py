"""Check a browser profile end to end, and optionally try a search.

    python scripts/browser_check.py research                  # start, attach, load a page
    python scripts/browser_check.py research --search "hydrogen storage"
    python scripts/browser_check.py research --capture        # save real result pages as test fixtures
    python scripts/browser_check.py --search "q"              # no profile: Tor on/off per SCOUT_TOR (asks if "ask")
    python scripts/browser_check.py --tor --search "q"        # force Tor on;  --no-tor forces it off

Steps are reported OK / FAIL in order (helper, tunnel, attach, page load,
search), so a broken setup points at the failing layer.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scout.browser import load_browser_config, open_browser  # noqa: E402
from scout.tools.scrub import scrub_html  # noqa: E402
from scout.tools.search import ENGINES, TorChoiceRequired, resolve_tor, search_with  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "search"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("profile", nargs="?", default=None)
    ap.add_argument("--search", metavar="QUERY")
    ap.add_argument("--capture", action="store_true", help="save each engine's first result page to tests/fixtures/search")
    tor = ap.add_mutually_exclusive_group()
    tor.add_argument("--tor", dest="tor", action="store_true", default=None, help="search through Tor")
    tor.add_argument("--no-tor", dest="tor", action="store_false", help="search directly, without Tor")
    a = ap.parse_args()

    cfg = load_browser_config()
    if a.profile:
        name = a.profile
    else:
        try:
            name, _ = resolve_tor(cfg, a.tor)
        except TorChoiceRequired:
            if not sys.stdin.isatty():
                print("SCOUT_TOR=ask: pass --tor or --no-tor (no terminal to ask on).")
                return 2
            answer = input("Search through Tor? [y/N] ").strip().lower()
            name, _ = resolve_tor(cfg, answer in ("y", "yes", "on"))
    profile = cfg.profile(name)
    print(f"Profile {name}: {profile.browser} on {profile.host.name} ({profile.host.kind}), port {profile.port}")
    if profile.host.live_view:
        print(f"Live view: {profile.host.live_view}")
    print()

    t0 = time.perf_counter()
    try:
        async with open_browser(profile, cfg) as b:
            print(f"  OK    start + attach   {time.perf_counter() - t0:5.1f}s  {b.endpoint.version}, "
                  f"{len(b.context.pages)} tab(s) already open")
            page = await b.new_page()
            t1 = time.perf_counter()
            await page.goto("https://example.org", wait_until="domcontentloaded", timeout=30000)
            print(f"  OK    page load        {time.perf_counter() - t1:5.1f}s  {await page.title()!r}")

            if profile.proxy:
                # Proxied profile: confirm where traffic leaves. Tor's own check
                # service says whether the address is a Tor exit.
                t3 = time.perf_counter()
                await page.goto("https://check.torproject.org/api/ip", wait_until="domcontentloaded", timeout=60000)
                info = json.loads(await page.inner_text("body"))
                ok = bool(info.get("IsTor")) if ":9050" in profile.proxy else True   # 9050 = the tor service
                print(f"  {'OK  ' if ok else 'FAIL'}  exit address     {time.perf_counter() - t3:5.1f}s  "
                      f"{info.get('IP')} (Tor exit: {'yes' if info.get('IsTor') else 'no'})")
                if not ok:
                    return 1

            if a.search:
                t2 = time.perf_counter()
                out = await search_with(b, a.search, cfg.search)
                tried = ", ".join(f"{x.engine}: {x.blocked or x.error or f'{x.results} results/{x.pages} pages'}"
                                  for x in out.attempts)
                status = "OK  " if out.results else "FAIL"
                print(f"  {status}  search           {time.perf_counter() - t2:5.1f}s  {tried}")
                for r in out.results[:10]:
                    print(f"          {r.rank:>2}. [{r.engine}] {r.title[:70]}\n              {r.url[:100]}")
                if out.needs_human:
                    print(f"\n  Every engine showed a bot check. Solve it in the live view: {out.live_view}")

            if a.capture:
                query = a.search or "hydrogen storage in natural gas pipelines"
                for eng in cfg.search.engines:
                    await page.goto(ENGINES[eng].url(query, 0), wait_until="domcontentloaded", timeout=30000)
                    await ENGINES[eng].prepare(page)                # e.g. decline Google's cookie consent
                    await page.wait_for_timeout(2500)              # let script-rendered results appear
                    blocked = await ENGINES[eng].blocked(page)
                    path = FIXTURES / f"{eng}_{'blocked' if blocked else 'live'}.html"
                    path.write_text(scrub_html(await page.content()))   # no location or session data
                    print(f"  {'FAIL' if blocked else 'OK  '}  capture {eng:<10}  -> {path.relative_to(ROOT)}"
                          + (f"  ({blocked})" if blocked else ""))
    except Exception as e:  # noqa: BLE001
        print(f"  FAIL  {time.perf_counter() - t0:5.1f}s  {type(e).__name__}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
