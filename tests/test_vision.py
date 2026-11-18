"""Phase 3 D2/D3: the vision loop on the browser page, its gates (submit approval, passwords, CAPTCHAs), the
check after each action, the page model's hint for drawn content, and the agent wiring. Local headless browser,
routed fixture pages; the vision model is faked (it is told where things are from the real element boxes)."""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest

from scout import vision
from scout.agent import SUB_EXCLUDED, TOOL_NAMES, Scout
from scout.approvals import ApprovalPolicy
from scout.browser.agent_tools import BrowserRunner
from scout.resilience import RetryPolicy
from scout.task import TaskRecord
from test_browser_tools import Fake
from test_phase1 import call, handler

CHART = """<!doctype html><html><head><title>Chart</title></head><body><main><h1>Sales</h1>
<canvas id=c width=600 height=300></canvas></main><script>
const g = document.getElementById('c').getContext('2d'); g.font = '20px sans-serif';
g.fillText('January', 40, 280); g.fillText('February', 200, 280); g.fillRect(50, 100, 60, 160);
</script></body></html>"""

WIDGET = """<!doctype html><html><head><title>Widget</title></head><body><main>
<div id=more style="position:absolute;left:100px;top:100px;width:160px;height:50px;background:#36c;color:#fff"
 onclick="document.getElementById('out').textContent='Opened details'">Show more</div>
<p id=out style="position:absolute;top:300px">Closed</p>
<div id=still style="position:absolute;left:400px;top:100px;width:100px;height:50px;background:#eee">Nothing</div>
</main></body></html>"""

FORM = """<!doctype html><html><head><title>Order</title></head><body><main>
<form action="/done" method=get>
<input id=name name=name placeholder="Your name" style="position:absolute;left:20px;top:20px;width:200px">
<input id=pw type=password name=pw style="position:absolute;left:20px;top:80px;width:200px">
<button id=go style="position:absolute;left:20px;top:140px;width:120px;height:40px">Go</button>
</form></main></body></html>"""

DONE = "<!doctype html><html><head><title>Done</title></head><body><main>Order placed</main></body></html>"

CAPTCHA = """<!doctype html><html><head><title>Check</title></head><body><main>
<iframe id=cf src="https://challenges.cloudflare.test/turnstile" title="Widget containing a Cloudflare security
challenge" style="position:absolute;left:20px;top:20px;width:300px;height:80px"></iframe></main></body></html>"""


async def routes(context):
    pages = {"/chart": CHART, "/widget": WIDGET, "/form": FORM, "/captcha": CAPTCHA}

    async def serve(route):
        path = "/" + route.request.url.split("/", 3)[3].split("?")[0]
        if path in pages:
            await route.fulfill(status=200, content_type="text/html", body=pages[path])
        elif path == "/done":
            await route.fulfill(status=200, content_type="text/html", body=DONE)
        else:
            await route.fulfill(status=404, body="")
    await context.route("https://v.test/**", serve)
    await context.route("https://challenges.cloudflare.test/**", lambda r: r.fulfill(status=200, body="<p>check</p>"))


class Eyes:
    """A fake vision model: answers questions with `answer`, and finds targets with `where(prompt)`."""

    def __init__(self, answer="", where=None):
        self.answer, self.where, self.calls = answer, where, []

    def chat(self, role, messages, **kw):
        assert role == "vision" and kw.get("max_tokens", 0) >= 512
        parts = messages[-1]["content"]
        assert parts[0]["image_url"]["url"].startswith("data:image/png;base64,")
        prompt = parts[1]["text"]
        self.calls.append(prompt)
        text = json.dumps(self.where(prompt)) if "Find:" in prompt else self.answer
        return SimpleNamespace(text=text, tool_calls=[], raw=None)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOUT_LIBRARY_DIR", str(tmp_path / "library"))
    monkeypatch.setenv("SCOUT_TASKS_DIR", str(tmp_path / "tasks"))
    monkeypatch.setenv("SCOUT_PLAYBOOKS_DIR", str(tmp_path / "playbooks"))


@pytest.fixture
def runner():
    r = BrowserRunner("local-test", setup=routes)
    yield r
    r.close()


def centre(runner, css: str) -> dict:
    async def go():
        return await runner.page.evaluate(
            "(s) => { const r = document.querySelector(s).getBoundingClientRect();"
            " return {x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2)}; }", css)
    return runner.run(go())


def tools(runner, eyes, approve=lambda s: True, tmp_path=None):
    return vision.VisionTools(llm=eyes, screen=runner.screen, approve=approve,
                              shots_dir=tmp_path / "shots" if tmp_path else None, actions=[])


# -- helpers ----------------------------------------------------------------------------------------------------

def test_png_size_and_json_parsing():
    png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x05\x00\x00\x00\x03\x20" + b"\0" * 8
    assert vision.png_size(png) == (1280, 800)
    with pytest.raises(ValueError):
        vision.png_size(b"GIF89a")
    assert vision.parse_json('<think>hm</think>```json\n{"x": 3, "y": 4}\n```') == {"x": 3, "y": 4}
    assert vision.parse_json('Sure: {"missing": "no"} done') == {"missing": "no"}
    assert vision.parse_json("no json") is None


# -- D3: the page model says when part of a page is drawn ---------------------------------------------------------

def test_drawn_content_is_flagged_for_vision(runner):
    out = runner.call("browser_open", {"url": "https://v.test/chart"}, approve=lambda s: False)
    assert "canvas 600x300" in out["visual"] and "vision_look" in out["visual"]
    assert "January" not in json.dumps(out["elements"]) + out["text"]          # the DOM cannot show the labels
    plain = runner.call("browser_open", {"url": "https://v.test/form"}, approve=lambda s: False)
    assert "visual" not in plain


# -- D2: look, act, check ---------------------------------------------------------------------------------------

def test_look_reads_the_screen(runner, tmp_path):
    runner.call("browser_open", {"url": "https://v.test/chart"}, approve=lambda s: False)
    eyes = Eyes(answer="<think>…</think>The x-axis labels are January and February.")
    out = tools(runner, eyes, tmp_path=tmp_path).call("vision_look", {"question": "What are the x-axis labels?"})
    assert out["answer"] == "The x-axis labels are January and February."
    assert out["screen"] == "browser" and out["title"] == "Chart" and out["screenshot"].endswith("vision-1.png")


def test_click_by_description_and_the_check_after_it(runner, tmp_path):
    runner.call("browser_open", {"url": "https://v.test/widget"}, approve=lambda s: False)
    target = centre(runner, "#more")
    eyes = Eyes(where=lambda p: {**target, "label": "Show more"})
    vt = tools(runner, eyes, tmp_path=tmp_path)
    out = vt.call("vision_click", {"target": "the blue Show more box"})
    assert out["changed"] is True and out["clicked"]["label"] == "Show more"
    assert runner.call("browser_read", {}, approve=lambda s: False)["text"].count("Opened details") == 1
    assert vt.actions[-1]["tool"] == "vision_click"
    still = centre(runner, "#still")
    eyes.where = lambda p: {**still, "label": "Nothing"}
    out = vt.call("vision_click", {"target": "the grey box"})
    assert out["changed"] is False and "did not change" in out["note"]


def test_missing_and_off_screen_targets_are_errors(runner):
    runner.call("browser_open", {"url": "https://v.test/widget"}, approve=lambda s: False)
    vt = tools(runner, Eyes(where=lambda p: {"missing": "no such button"}))
    assert "no such button" in vt.call("vision_click", {"target": "the red button"})["error"]
    vt.llm.where = lambda p: {"x": 99999, "y": 5}
    assert "outside" in vt.call("vision_click", {"target": "the red button"})["error"]
    vt.llm.where = lambda p: "not an object"
    assert "error" in vt.call("vision_click", {"target": "the red button"})


# -- gates --------------------------------------------------------------------------------------------------------

def test_a_submit_found_by_vision_needs_approval(runner):
    runner.call("browser_open", {"url": "https://v.test/form"}, approve=lambda s: False)
    go = centre(runner, "#go")
    asked = []
    vt = tools(runner, Eyes(where=lambda p: {**go, "label": "Go"}), approve=lambda s: asked.append(s) or False)
    out = vt.call("vision_click", {"target": "the grey button under the fields"})     # innocent words, but a submit
    assert out["decision"] == "denied" and len(asked) == 1
    assert runner.screen().state()["title"] == "Order"                                # nothing was clicked
    vt.approve = lambda s: asked.append(s) or True
    out = vt.call("vision_click", {"target": "the Go button", "action_summary": "Place the order"})
    assert out["decision"] == "approved" and asked[-1] == "Place the order" and out["title"] == "Done"


def test_enter_in_a_form_needs_approval(runner):
    runner.call("browser_open", {"url": "https://v.test/form"}, approve=lambda s: False)
    name = centre(runner, "#name")
    asked = []
    vt = tools(runner, Eyes(where=lambda p: {**name, "label": "Your name"}), approve=lambda s: asked.append(s) or False)
    assert vt.call("vision_type", {"target": "the name field", "text": "Test User"})["typed"] == 9
    out = vt.call("vision_key", {"key": "Enter"})
    assert out["decision"] == "denied" and asked and runner.screen().state()["title"] == "Order"
    assert "unknown key" in vt.call("vision_key", {"key": "ctrl+w"})["error"]


def test_passwords_are_never_typed(runner):
    runner.call("browser_open", {"url": "https://v.test/form"}, approve=lambda s: False)
    pw = centre(runner, "#pw")
    eyes = Eyes(where=lambda p: {**pw, "label": ""})
    vt = tools(runner, eyes)
    assert "never types passwords" in vt.call("vision_type", {"target": "the Password box", "text": "x"})["error"]
    assert not eyes.calls                                             # refused before looking
    out = vt.call("vision_type", {"target": "the second field", "text": "hunter2"})   # the DOM knows better
    assert "password field" in out["error"]
    runner.run(runner.page.focus("#pw"))
    out = vt.call("vision_type", {"target": "", "text": "hunter2"})  # typing into whatever has focus
    assert "password field" in out["error"]

    async def value():
        return await runner.page.eval_on_selector("#pw", "e => e.value")
    assert runner.run(value()) == ""


def test_captchas_are_never_solved(runner):
    runner.call("browser_open", {"url": "https://v.test/captcha"}, approve=lambda s: False)
    eyes = Eyes(where=lambda p: {"captcha": True})
    vt = tools(runner, eyes)
    assert "hand_to_human" in vt.call("vision_click", {"target": "the I'm not a robot checkbox"})["error"]
    assert not eyes.calls
    assert "hand_to_human" in vt.call("vision_click", {"target": "the checkbox"})["error"]
    box = centre(runner, "#cf")
    eyes.where = lambda p: {**box, "label": "Verify"}
    assert "CAPTCHA" in vt.call("vision_click", {"target": "the checkbox"})["error"]   # the frame is a challenge


# -- the agent --------------------------------------------------------------------------------------------------

class Both(Fake):
    """The scripted planner plus a fake vision model on the same client."""

    def __init__(self, script, eyes):
        super().__init__(script)
        self.eyes = eyes

    def chat(self, role, messages, **kw):
        return self.eyes.chat(role, messages, **kw) if role == "vision" else super().chat(role, messages, **kw)


def test_the_agent_reads_a_canvas_with_vision():
    @contextmanager
    def client(tor=False):
        with httpx.Client(transport=httpx.MockTransport(handler)) as c:
            yield c
    eyes = Eyes(answer="January, February")
    llm = Both([[call("browser_open", url="https://v.test/chart")],
                [call("vision_look", question="What are the x-axis labels?")],
                [call("finish", report="[done] January, February")]], eyes)
    rec = Scout(TaskRecord.create("Report the x-axis labels of the chart on https://v.test/chart", tor=False),
                llm=llm, client_factory=client, browser_profile="local-test", browser_setup=routes,
                ask_approval=lambda s: False, approvals=ApprovalPolicy("ask"), verify=False,
                model_policy=RetryPolicy(attempts=2, base=0, sleep=lambda s: None)).run()
    assert [s.tool for s in rec.steps] == ["browser_open", "vision_look", "finish"] and rec.status == "done"
    looked = json.loads([m for m in rec.messages if m["role"] == "tool"][1]["content"])
    assert looked["answer"] == "January, February" and (rec.dir / "screenshots" / "vision-1.png").exists()
    assert {"vision_look", "vision_click"} <= set(TOOL_NAMES) and "vision_click" in SUB_EXCLUDED
