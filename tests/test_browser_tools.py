"""Phase 2 browser tools through the agent: forms, the submit gate in every approval mode, password refusal,
stale ids, cookie banners. Local headless browser; fixture pages served by routes; scripted fake model."""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest

from scout.agent import Scout
from scout.approvals import ApprovalPolicy
from scout.resilience import RetryPolicy
from scout.task import TaskRecord
from test_phase1 import call, handler


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOUT_LIBRARY_DIR", str(tmp_path / "library"))
    monkeypatch.setenv("SCOUT_TASKS_DIR", str(tmp_path / "tasks"))


ORDER = """<!doctype html><html><head><title>Pizza order</title></head><body>
<div style="position:fixed;bottom:0;width:100%;height:50px">We use cookies. <button>Reject all</button></div>
<form method="post" action="/post">
 <label for="n">Customer name</label><input id="n" name="custname">
 <label for="s">Size</label><select id="s" name="size"><option>Small</option><option>Medium</option><option>Large</option></select>
 <label><input type="checkbox" name="topping" value="mushroom"> Mushroom</label>
 <button type="submit">Submit order</button>
</form></body></html>"""
LOGIN = """<!doctype html><html><head><title>Login</title></head><body><form method="post" action="/auth">
<label for="u">Username</label><input id="u" name="u"><label for="p">Password</label><input id="p" type="password" name="p">
<button type="submit">Login</button></form></body></html>"""


async def routes(context):
    async def serve(route):
        req = route.request
        if req.url.endswith("/order"):
            await route.fulfill(status=200, content_type="text/html", body=ORDER)
        elif req.url.endswith("/login"):
            await route.fulfill(status=200, content_type="text/html", body=LOGIN)
        elif req.url.endswith("/post") and req.method == "POST":
            form = parse_qs(req.post_data or "")
            body = "<html><head><title>Order received</title></head><body><main><h1>Order received</h1><pre>" + \
                   json.dumps(form) + "</pre></main></body></html>"
            await route.fulfill(status=200, content_type="text/html", body=body)
        else:
            await route.fulfill(status=404, body="not found")
    await context.route("https://forms.test/**", serve)


class Fake:
    """Scripted planner whose later turns can depend on the page ids it has seen (functions of the transcript)."""

    def __init__(self, script):
        self.script = list(script)

    def chat(self, role, messages, **kw):
        turn = self.script.pop(0) if self.script else "…"
        if callable(turn):
            turn = turn(messages)
        msg = (SimpleNamespace(content="", tool_calls=turn) if isinstance(turn, list)
               else SimpleNamespace(content=turn, tool_calls=None))
        return SimpleNamespace(message=msg, text=msg.content or "", tool_calls=list(msg.tool_calls or []),
                               raw=SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop")]))


def last_page(messages) -> dict:
    """The most recent tool result that is a page listing (refused calls return errors instead)."""
    for m in reversed([m for m in messages if m["role"] == "tool"]):
        try:
            d = json.loads(m["content"])
        except ValueError:
            continue
        if isinstance(d, dict) and "elements" in d:
            return d
    raise AssertionError("no page has been read yet")


def eid(messages, needle: str) -> str:
    for line in last_page(messages)["elements"]:
        if needle in line:
            return line.split()[0]
    raise AssertionError(f"{needle!r} not on the page: {last_page(messages)['elements']}")


def run(script, *, approvals: ApprovalPolicy | None = None, ask=lambda s: False):
    @contextmanager
    def client(tor=False):
        with httpx.Client(transport=httpx.MockTransport(handler)) as c:
            yield c
    rec = Scout(TaskRecord.create("Order a medium pizza with mushrooms for Test User.", tor=False), llm=Fake(script),
                client_factory=client, browser_profile="local-test", browser_setup=routes, ask_approval=ask,
                approvals=approvals or ApprovalPolicy("ask"),
                model_policy=RetryPolicy(attempts=2, base=0, sleep=lambda s: None)).run()
    return rec


def order_script(final_report="[done] ordered"):
    return [
        [call("browser_open", url="https://forms.test/order")],
        lambda m: [call("browser_click", element_id=eid(m, '"Reject all"'))],
        lambda m: [call("browser_type", element_id=eid(m, '"Customer name"'), text="Test User")],
        lambda m: [call("browser_select", element_id=eid(m, '"Size"'), option="medium")],
        lambda m: [call("browser_click", element_id=eid(m, "Mushroom"))],
        lambda m: [call("browser_click", element_id=eid(m, '"Submit order"'))],          # refused: must use click_submit
        lambda m: [call("click_submit", element_id=eid(m, '"Submit order"'),
                        action_summary="Submit the pizza order for Test User")],
        [call("finish", report=final_report)],
        [call("finish", report=final_report)],          # in case the finish is challenged after the refused click
    ]


def test_form_submitted_after_human_approval():
    asked = []
    rec = run(order_script(), ask=lambda s: asked.append(s) or True)
    tools = [s.tool for s in rec.steps]
    assert asked == ["Submit the pizza order for Test User"]
    assert tools.index("request_approval") == tools.index("click_submit") - 1     # gate logged right before the click
    refused = rec.steps[tools.index("request_approval") - 1]          # the plain click on "Submit order"
    assert refused.tool == "browser_click"
    assert not refused.ok and "use click_submit" in refused.summary
    submit = rec.steps[tools.index("click_submit")]
    assert submit.ok and "Order received" in submit.summary
    page = last_page(rec.messages)                                    # the confirmation page after submitting
    assert "Medium" in page["text"] and "Test User" in page["text"] and "mushroom" in page["text"]
    assert rec.state["approvals"][0]["by"] == "human"


def test_denied_approval_clicks_nothing():
    rec = run(order_script("[not done] approval denied"), ask=lambda s: False)
    submit = next(s for s in rec.steps if s.tool == "click_submit")
    assert '"denied"' in submit.summary and "Order received" not in submit.summary


def test_auto_mode_needs_no_human_and_records_it():
    rec = run(order_script(), approvals=ApprovalPolicy("auto"),
              ask=lambda s: pytest.fail("a human was asked in auto mode"))
    submit = next(s for s in rec.steps if s.tool == "click_submit")
    assert submit.ok and "Order received" in submit.summary
    assert rec.state["approvals"][0] == {"action": "Submit the pizza order for Test User",
                                         "url": "https://forms.test/order", "approved": True, "by": "auto"}


def test_auto_mode_respects_domain_allowlist():
    rec = run(order_script("[not done] site not allowed"), approvals=ApprovalPolicy("auto", domains=["example.org"]))
    submit = next(s for s in rec.steps if s.tool == "click_submit")
    assert '"denied"' in submit.summary and "SCOUT_AUTO_APPROVE_DOMAINS" in rec.state["approvals"][0]["by"]


def test_deny_mode_is_a_dry_run():
    rec = run(order_script("[partial] filled, not submitted"), approvals=ApprovalPolicy("deny"),
              ask=lambda s: pytest.fail("a human was asked in deny mode"))
    assert "Order received" not in next(s for s in rec.steps if s.tool == "click_submit").summary


def test_password_fields_are_refused_in_every_mode():
    script = [[call("browser_open", url="https://forms.test/login")],
              lambda m: [call("browser_type", element_id=eid(m, '"Password"'), text="hunter2")],
              [call("hand_to_human", reason="The site needs a sign-in.")]]
    rec = run(script, approvals=ApprovalPolicy("auto"))
    typed = rec.steps[1]
    assert not typed.ok and "never enters passwords" in typed.summary
    assert rec.status == "needs_human"
    assert "sign_in" in rec.steps[0].summary or "password" in rec.steps[0].summary.lower()


def test_unknown_element_id_asks_to_reread():
    rec = run([[call("browser_open", url="https://forms.test/order")],
               [call("browser_click", element_id="e999")],
               [call("finish", report="[not done]")], [call("finish", report="[not done] bad id")]])
    assert not rec.steps[1].ok and "browser_read" in rec.steps[1].summary


def test_payment_actions_denied_in_auto_unless_enabled():
    p = ApprovalPolicy("auto")
    assert not p.decide("Pay 25 EUR with the saved card", "https://shop.example/checkout", lambda s: True).approved
    assert ApprovalPolicy("auto", allow_payments=True).decide("Pay 25 EUR", "https://shop.example", lambda s: True).approved
    assert p.decide("Submit the application", "https://jobs.example/apply", lambda s: False).approved
    with pytest.raises(ValueError):
        ApprovalPolicy("sometimes")
