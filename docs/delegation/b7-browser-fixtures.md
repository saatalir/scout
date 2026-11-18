---
files: ["tests/browser_fixtures.py", "tests/fixtures/browser/*.html", "tests/test_browser_resilience.py"]
context: ["scout/browser/agent_tools.py", "scout/browser/page.py", "tests/test_browser_more.py"]
tests: ["tests/test_browser_resilience.py"]
---
# B7: misbehaving browser fixture pages, and tests of the browser runner's resilience

Write fixture pages that misbehave on purpose, a small helper that serves them to the local headless browser,
and tests that drive `scout.browser.agent_tools.BrowserRunner` directly against them. **You may not change
Scout's code**: the tests describe how the runner behaves today (read `agent_tools.py` carefully). If you
believe the runner is wrong for a case, still write the test for the behaviour described below.

## `tests/browser_fixtures.py`

```python
FIXTURES = Path(__file__).parent / "fixtures" / "browser"
BASE = "https://pages.test"

def make_routes(behaviour: dict | None = None):
    """Return an async `setup(context)` for BrowserRunner(setup=...): routes BASE/** to the fixture files.
    `/<name>` (with or without .html) serves FIXTURES/<name>.html as text/html.
    Query parameters change the response:
      ?delay=<seconds>   wait before answering (asyncio.sleep)
      ?status=<code>     answer with that HTTP status (the file's body; a plain body if no file)
      ?flaky=<n>         abort the first n requests to this path (route.abort("failed")), then serve it
    `behaviour` may map a path to a callable(route) -> awaitable to override everything for that path.
    Unknown paths: 404 "not found"."""
```

Counting for `flaky` is per `make_routes` call (a dict in the closure).

## Fixture pages (`tests/fixtures/browser/*.html`, self-contained, inline scripts only)

| file | behaviour |
| --- | --- |
| `slow.html` | plain page, title "Slow page", `<main><p>Finally loaded.</p></main>` (served with `?delay=3`) |
| `late_content.html` | a button "First"; after 1200 ms a script appends a button "Late button" |
| `rerender.html` | a list of three buttons "Item 1".."Item 3" and a button "Shuffle" whose onclick replaces the whole list with **new** button elements "Item A".."Item C" |
| `overlay.html` | a button "Hidden behind" fully covered by a fixed full-screen transparent `<div>` (pointer events on); a button "Remove overlay" above it (z-index higher) that removes the overlay |
| `error.html` | title "Server error", `<main><h1>Something went wrong</h1></main>` (served with `?status=500`) |
| `infinite.html` | 10 links "Story 1".."Story 10" in a tall page (each ~200px high); on scroll near the bottom, a script appends 10 more ("Story 11".. ) |
| `opener.html` | a button "Open window" with `onclick="window.open('/child.html')"` and a link "Child in a tab" with `target=_blank` to `/child.html` |
| `child.html` | title "Child", `<main><p>Child page</p></main>` |
| `pushstate.html` | a button "Next step" whose onclick calls `history.pushState({}, '', '/pushstate.html?step=2')` and changes the `<h1>` to "Step 2" |
| `late_banner.html` | after 800 ms adds a fixed bottom div "We use cookies. <button>Reject all</button>" |
| `disabled.html` | a disabled button "Not yet" and text input "Name" |

## Tests: `tests/test_browser_resilience.py`

```python
runner = BrowserRunner("local-test", setup=make_routes())
try:
    out = runner.call("browser_open", {"url": f"{BASE}/slow?delay=3"}, approve=lambda s: False)
finally:
    runner.close()
```

Use a fixture or helper so every test closes its runner. Element ids come from `out["elements"]` lines
(`"e3 button \"Shuffle\" ..."`, the id is the first word); write a helper `eid(out, needle)`. Results are dicts.
Use `tmp_path` and an autouse fixture setting `SCOUT_LIBRARY_DIR` and `SCOUT_TASKS_DIR` like
`tests/test_browser_more.py`. No network.

One test each:
1. **slow page**: `browser_open` of `/slow?delay=3` succeeds: no `"error"`, `"Finally loaded."` in `out["text"]`.
2. **flaky navigation**: `/slow?flaky=1` succeeds on retry (no `"error"`, title "Slow page").
3. **permanently failing navigation**: `/slow?flaky=10` returns a dict with `"error"` starting `"could not open"`.
4. **HTTP error page**: `/error?status=500` returns `out["http_status"] == 500` and the page text.
5. **late content**: open `/late_content`; "Late button" is not in the elements; sleep 1.5 s; `browser_read`
   lists it, and the id of "First" is the same in both reads.
6. **re-render invalidates ids**: open `/rerender`, click "Shuffle"; the result lists "Item A"; then
   `browser_click` with the old id of "Item 1" returns an `"error"` that mentions `browser_read`.
7. **unknown id**: `browser_click` with `"e999"` returns an error mentioning `browser_read`.
8. **covered element**: on `/overlay`, clicking "Hidden behind" returns an error containing
   `"did not respond in time"` (this takes ~15 s; mark it `@pytest.mark.slow` and register the marker in the
   test module with `pytestmark`-free usage: just use `pytest.mark.slow` and add
   `def pytest_configure(config)`-free code — simply do not filter it); after clicking "Remove overlay", clicking
   "Hidden behind" succeeds.
9. **infinite scroll**: open `/infinite`; "Story 15" not listed; after `browser_scroll` (down) up to 4 times,
   "Story 15" is listed.
10. **window.open popup is followed**: click "Open window": result has `"note"` mentioning a new tab or popup and
    title "Child"; `browser_back` returns to the opener (title of `opener.html`, `"note"` contains "Closed").
11. **target=_blank tab is followed** the same way.
12. **pushState**: clicking "Next step" gives `out["url"]` ending `step=2` and text containing "Step 2".
13. **late cookie banner**: open `/late_banner`; after 1.2 s `browser_read` has `"cookie_banner"` in the result.
14. **disabled element**: the elements line for "Not yet" contains `disabled`.
15. **browser_back with no history**: on a fresh runner, `browser_back` returns an `"error"`
    (`"there is no previous page"`) or a dict without crashing; assert no exception.

Keep the whole module under ~60 s except the covered-element test.
