"""B1 page model: element listing, labels, stable ids, safety (passwords), banners — on a local headless browser."""

from __future__ import annotations

import asyncio

from scout.browser import open_browser
from scout.browser.page import locator_for, snapshot

PAGE = """<!doctype html><html><head><title>Apply</title></head><body>
<div id="cookies" style="position:fixed;bottom:0;left:0;right:0;height:60px;background:#eee">
  We use cookies to improve your experience. <button>Accept all</button> <button>Reject all</button></div>
<main><h1>Job application</h1><p>Fill in the form below.</p>
<a href="/jobs/42">Back to the job</a>
<form action="/apply" method="post">
  <label for="name">Full name</label><input id="name" name="name" required>
  <input name="email" type="email" placeholder="Email address">
  <label>Experience <select name="exp"><option>0-2 years</option><option>3-5 years</option></select></label>
  <label><input type="checkbox" name="terms"> I accept the terms</label>
  <input type="password" name="pw" aria-label="Password" value="secret123">
  <input type="hidden" name="csrf" value="tok">
  <button type="submit">Submit application</button>
  <button style="display:none">Hidden</button>
  <div aria-hidden="true"><button>Decor</button></div>
</form></main></body></html>"""


def run(coro):
    return asyncio.run(coro)


async def with_page(fn):
    async with open_browser("local-test") as b:
        page = await b.new_page()
        await page.set_content(PAGE)
        return await fn(page)


def test_snapshot_lists_visible_interactive_elements_with_labels():
    async def go(page):
        return await snapshot(page)
    v = run(with_page(go))
    names = {e.name for e in v.elements}
    assert {"Full name", "Email address", "Password", "Submit application", "Back to the job",
            "Accept all", "Reject all"} <= names
    assert any(e.name.startswith("Experience") and e.options == ["0-2 years", "3-5 years"] for e in v.elements)
    assert not {"Hidden", "Decor"} & names                       # display:none and aria-hidden are skipped
    assert all(e.type != "hidden" for e in v.elements)
    name = next(e for e in v.elements if e.name == "Full name")
    assert name.required and name.form == "f1"
    terms = next(e for e in v.elements if e.type == "checkbox")
    assert terms.checked is False and "accept the terms" in terms.name
    assert v.title == "Apply" and "Fill in the form below." in v.text
    assert v.forms[0]["method"] == "post"


def test_password_value_is_never_read():
    async def go(page):
        return await snapshot(page)
    v = run(with_page(go))
    pw = next(e for e in v.elements if e.type == "password")
    assert pw.value == "(filled)" and "secret123" not in str(v.for_model())
    assert "sign_in" in v.for_model()


def test_cookie_banner_is_flagged():
    async def go(page):
        return await snapshot(page)
    v = run(with_page(go))
    assert v.cookie_banner and "decline non-essential" in v.for_model()["cookie_banner"]


def test_ids_are_stable_and_new_elements_get_new_ids():
    async def go(page):
        first = await snapshot(page)
        await page.evaluate("document.querySelector('main').insertAdjacentHTML('beforeend', '<button>Late button</button>')")
        second = await snapshot(page)
        return first, second
    first, second = run(with_page(go))
    by_name_1 = {e.name: e.id for e in first.elements}
    by_name_2 = {e.name: e.id for e in second.elements}
    assert all(by_name_2[n] == i for n, i in by_name_1.items())
    assert by_name_2["Late button"] not in by_name_1.values()


def test_locator_acts_on_the_snapshot_id():
    async def go(page):
        v = await snapshot(page)
        eid = next(e.id for e in v.elements if e.name == "Full name")
        await locator_for(page, eid).fill("Test User")
        return (await snapshot(page)).element(eid).value
    assert run(with_page(go)) == "Test User"


def test_element_line_is_compact():
    async def go(page):
        return await snapshot(page)
    v = run(with_page(go))
    line = next(e for e in v.elements if e.name == "Back to the job").line()
    assert line.startswith("e") and ' a "Back to the job" -> ' in line
