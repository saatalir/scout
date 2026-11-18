"""Phase 2 B2-B4 additions: browser downloads (verified, saved), new tabs and popups, JavaScript dialogs,
screenshots, and resuming after a hand-off to a person. Local headless browser; routed fixture pages."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from scout.agent import Scout
from scout.approvals import ApprovalPolicy
from scout.library import read_front_matter
from scout.resilience import RetryPolicy
from scout.task import TaskRecord
from test_browser_tools import Fake, eid, last_page
from test_phase1 import call, handler, make_pdf


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOUT_LIBRARY_DIR", str(tmp_path / "library"))
    monkeypatch.setenv("SCOUT_TASKS_DIR", str(tmp_path / "tasks"))


FILES = """<!doctype html><html><head><title>Member files</title></head><body><main>
<h1>Member files</h1><p id="status">3 files</p>
<a href="/report.pdf">Download annual report</a>
<a href="/fake.pdf">Download broken file</a>
<a href="/report2.pdf" target="_blank">Report (opens a tab)</a>
<a href="/details" target="_blank">Details in a new tab</a>
<a href="/plain">Plain page</a>
<button onclick="if (confirm('Clear the list?')) document.getElementById('status').textContent = 'cleared'">Clear list</button>
<button onclick="alert('Saved for later'); document.getElementById('status').textContent = 'noted'">Remember</button>
</main></body></html>"""
DETAILS = "<html><head><title>Details</title></head><body><main><h1>Report details</h1><p>42 pages.</p></main></body></html>"
PLAIN = "<html><head><title>Plain</title></head><body><main><p>Nothing to download.</p></main></body></html>"
LOGIN = """<html><head><title>Sign in</title></head><body><form><label for="u">User</label><input id="u">
<label for="p">Password</label><input id="p" type="password"><button>Sign in</button></form></body></html>"""


async def routes(context):
    async def serve(route):
        url = route.request.url
        if url.endswith("/files"):
            await route.fulfill(status=200, content_type="text/html", body=FILES)
        elif url.endswith("/report.pdf"):
            await route.fulfill(status=200, body=make_pdf("Annual Report 2025"), headers={
                "content-type": "application/pdf", "content-disposition": 'attachment; filename="annual-report.pdf"'})
        elif url.endswith("/report2.pdf"):
            await route.fulfill(status=200, body=make_pdf("Annual Report 2025"), headers={
                "content-type": "application/pdf", "content-disposition": 'attachment; filename="report2.pdf"'})
        elif url.endswith("/fake.pdf"):
            await route.fulfill(status=200, body="<html><title>Sign in</title>" + "x" * 400, headers={
                "content-type": "application/octet-stream", "content-disposition": 'attachment; filename="f.pdf"'})
        elif url.endswith("/details"):
            await route.fulfill(status=200, content_type="text/html", body=DETAILS)
        elif url.endswith("/plain"):
            await route.fulfill(status=200, content_type="text/html", body=PLAIN)
        elif url.endswith("/login"):
            await route.fulfill(status=200, content_type="text/html", body=LOGIN)
        else:
            await route.fulfill(status=404, body="not found")
    await context.route("https://members.test/**", serve)


@contextmanager
def client(tor=False):
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        yield c


def scout(rec, script):
    return Scout(rec, llm=Fake(script), client_factory=client, browser_profile="local-test", browser_setup=routes,
                 approvals=ApprovalPolicy("ask"), ask_approval=lambda s: False,
                 model_policy=RetryPolicy(attempts=2, base=0, sleep=lambda s: None), verify=False)


def new(objective="Get the annual report from the member area."):
    return TaskRecord.create(objective, tor=False)


def tool_result(rec, n: int) -> str:
    return rec.steps[n].summary


def test_browser_download_is_verified_and_saved():
    def save(m):
        import json
        path = next(json.loads(x["content"])["path"] for x in reversed(m)
                    if x["role"] == "tool" and '"path"' in x["content"])
        return [call("save_to_library", title="Annual Report 2025", kind="other", source="web",
                     source_url="https://members.test/report.pdf", download_path=path)]
    rec = scout(new(), [
        [call("browser_open", url="https://members.test/files")],
        lambda m: [call("browser_download", element_id=eid(m, "Download annual report"), expected_kind="pdf",
                        expected_title="Annual Report 2025")],
        save,
        [call("finish", report="[done] saved")]]).run()
    dl = rec.steps[1]
    assert dl.ok and '"verified": true' in dl.summary and "annual-report.pdf" in dl.summary
    meta = read_front_matter(Path(rec.files[0]))
    assert meta["file"] == "annual-report.pdf" and meta["sha256"]


def test_download_from_a_link_that_opens_a_tab():
    rec = scout(new(), [
        [call("browser_open", url="https://members.test/files")],
        lambda m: [call("browser_download", element_id=eid(m, "Report (opens a tab)"), expected_kind="pdf")],
        [call("browser_read")],
        [call("finish", report="[done]")]]).run()
    assert rec.steps[1].ok and "report2.pdf" in rec.steps[1].summary
    assert "Member files" in rec.steps[2].summary                    # still on the page, not a blank tab


def test_browser_download_failing_verification_is_not_kept():
    rec = scout(new(), [
        [call("browser_open", url="https://members.test/files")],
        lambda m: [call("browser_download", element_id=eid(m, "Download broken file"), expected_kind="pdf")],
        [call("finish", report="[not done]")], [call("finish", report="[not done] broken file")]]).run()
    assert not rec.steps[1].ok and "HTML page" in rec.steps[1].summary
    assert list(rec.downloads.glob("*")) == []


def test_link_without_download_says_what_opened():
    rec = scout(new(), [
        [call("browser_open", url="https://members.test/files")],
        lambda m: [call("browser_download", element_id=eid(m, "Plain page"))],
        [call("finish", report="[not done]")], [call("finish", report="[not done] no file")]]).run()
    s = rec.steps[1]
    assert not s.ok and "started no download" in s.summary and "members.test/plain" in s.summary


def test_new_tab_is_followed_and_back_returns():
    rec = scout(new(), [
        [call("browser_open", url="https://members.test/files")],
        lambda m: [call("browser_click", element_id=eid(m, "Details in a new tab"))],
        [call("browser_back")],
        [call("finish", report="[done]")]]).run()
    assert "Report details" in tool_result(rec, 1) and "new tab" in tool_result(rec, 1)
    assert "Member files" in tool_result(rec, 2) and "Closed the tab" in tool_result(rec, 2)


def test_confirm_dialogs_are_declined_and_alerts_reported():
    rec = scout(new(), [
        [call("browser_open", url="https://members.test/files")],
        lambda m: [call("browser_click", element_id=eid(m, "Clear list"))],
        lambda m: [call("browser_click", element_id=eid(m, "Remember"))],
        [call("finish", report="[done]")]]).run()
    cleared, noted = rec.steps[1].summary, rec.steps[2].summary
    assert "confirm declined: Clear the list?" in cleared and "cleared" not in cleared.split("confirm")[0]
    assert "alert accepted: Saved for later" in noted
    page = last_page(rec.messages)
    assert "noted" in page["text"]


def test_screenshot_is_saved_in_the_task_folder():
    rec = scout(new(), [[call("browser_open", url="https://members.test/files")], [call("browser_screenshot")],
                        [call("finish", report="[done]")]]).run()
    shots = list((rec.dir / "screenshots").glob("*.png"))
    assert len(shots) == 1 and shots[0].stat().st_size > 1000


def test_resume_after_hand_off_continues_where_the_browser_was():
    rec = scout(new("Download the member report (needs a sign-in)."), [
        [call("browser_open", url="https://members.test/login")],
        [call("hand_to_human", reason="The member area needs a sign-in.")]]).run()
    assert rec.status == "needs_human"
    assert f"python -m scout resume {rec.id}" in rec.report and "members.test/login" in rec.report
    assert rec.state["handed_over"]["url"] == "https://members.test/login"

    llm = Fake([[call("finish", report="[done] after sign-in")]])
    seen = []
    orig = llm.chat
    llm.chat = lambda role, messages, **kw: (seen.append([dict(x) for x in messages]), orig(role, messages, **kw))[1]
    again = Scout(TaskRecord.load(rec.id), llm=llm, client_factory=client, browser_profile="local-test",
                  browser_setup=routes, model_policy=RetryPolicy(attempts=2, base=0, sleep=lambda s: None),
                  verify=False).run()
    nudge = seen[0][-1]
    assert nudge["role"] == "user" and "finished what you handed over" in nudge["content"]
    assert "members.test/login" in nudge["content"] and again.status == "done"
    assert "handed_over" not in again.state
