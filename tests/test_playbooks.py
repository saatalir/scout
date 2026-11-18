"""B5 playbooks: parsing, matching, label lookup, running through the agent with its gates, the hand-back to the
model when a step fails, and traces of successful runs. Local headless browser; routed fixture pages."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
import yaml

from scout import playbooks
from scout.agent import Scout
from scout.approvals import ApprovalPolicy
from scout.browser.page import Element, PageView
from scout.resilience import RetryPolicy
from scout.task import TaskRecord
from test_browser_tools import Fake
from test_phase1 import call, handler

ROOT = Path(__file__).resolve().parent.parent

PIZZA = """<!doctype html><html><head><title>Pizza</title></head><body><form method="post" action="/post">
<p><label>Customer name: <input name="custname"></label></p>
<p><label>Telephone: <input type=tel name="custtel"></label></p>
<fieldset><legend>Pizza Size</legend>
<p><label> <input type=radio name=size value="small"> Small </label></p>
<p><label> <input type=radio name=size value="medium"> Medium </label></p></fieldset>
<fieldset><legend>Pizza Toppings</legend>
<p><label> <input type=checkbox name="topping" value="cheese"> Extra Cheese </label></p>
<p><label> <input type=checkbox name="topping" value="mushroom"> Mushroom </label></p></fieldset>
<p><label>Secret: <input type=password name="pw"></label></p>
<p><button>Submit order</button></p></form></body></html>"""

BOOK = {
    "name": "test-pizza", "description": "Order a pizza on the test form.",
    "match": {"urls": ["forms.test/pizza"]},
    "params": {"custname": {"description": "name", "required": True}, "phone": {"description": "phone"},
               "size": {"required": True}, "toppings": {"type": "list"}},
    "steps": [
        {"open": "https://forms.test/pizza"},
        {"type": {"field": "Customer name", "text": "{custname}"}},
        {"type": {"field": "Telephone", "text": "{phone}"}, "optional": True},
        {"check": {"field": "{size}"}},
        {"check": {"field": "{item}"}, "for_each": "toppings"},
        {"submit": {"element": "Submit order", "summary": "Submit the pizza order for {custname}"}},
        {"expect": {"text": "custname"}},
        {"fuzzy": "Report what the site returned."}]}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOUT_LIBRARY_DIR", str(tmp_path / "library"))
    monkeypatch.setenv("SCOUT_TASKS_DIR", str(tmp_path / "tasks"))
    books = tmp_path / "playbooks"
    books.mkdir()
    monkeypatch.setenv("SCOUT_PLAYBOOKS_DIR", str(books))
    return books


def write(books: Path, data: dict) -> None:
    (books / f"{data['name']}.yaml").write_text(yaml.safe_dump(data, sort_keys=False))


async def routes(context):
    async def serve(route):
        req = route.request
        if req.url.endswith("/pizza"):
            await route.fulfill(status=200, content_type="text/html", body=PIZZA)
        elif req.url.endswith("/post") and req.method == "POST":
            body = ("<html><head><title>Order received</title></head><body><main><pre>"
                    + json.dumps(parse_qs(req.post_data or "")) + "</pre></main></body></html>")
            await route.fulfill(status=200, content_type="text/html", body=body)
        else:
            await route.fulfill(status=404, body="not found")
    await context.route("https://forms.test/**", serve)


def run(script, objective="Order a medium pizza with mushrooms for Test User on https://forms.test/pizza",
        ask=lambda s: True, seen=None):
    @contextmanager
    def client(tor=False):
        with httpx.Client(transport=httpx.MockTransport(handler)) as c:
            yield c
    llm = Fake(script)
    if seen is not None:
        orig = llm.chat
        llm.chat = lambda role, messages, **kw: (seen.append([dict(m) for m in messages]), orig(role, messages, **kw))[1]
    return Scout(TaskRecord.create(objective, tor=False), llm=llm, client_factory=client, browser_profile="local-test",
                 browser_setup=routes, ask_approval=ask, approvals=ApprovalPolicy("ask"), verify=False,
                 model_policy=RetryPolicy(attempts=2, base=0, sleep=lambda s: None)).run()


def tool_results(rec) -> list[dict]:
    out = []
    for m in rec.messages:
        if m["role"] == "tool":
            try:
                out.append(json.loads(m["content"]))
            except ValueError:
                out.append({"raw": m["content"]})
    return out


# -- parsing and matching -------------------------------------------------------------------------------------

def test_shipped_playbooks_parse_and_match_their_eval_tasks():
    tasks = {t["id"]: t for t in yaml.safe_load((ROOT / "evals/tasks/tasks.yaml").read_text())["tasks"]}
    books = {p.name: p for p in playbooks.load_playbooks(ROOT / "playbooks")}
    assert books["httpbin-pizza"].matches(tasks["f01-form-with-approval"]["objective"])
    assert books["selenium-web-form"].matches(tasks["f03-multi-field-form"]["objective"])
    assert not books["httpbin-pizza"].matches(tasks["r01-known-paper-pdf"]["objective"])


def test_bad_playbooks_are_refused_with_the_reason():
    with pytest.raises(playbooks.PlaybookError, match="exactly one"):
        playbooks.parse_playbook({"name": "x", "steps": [{"open": "u", "type": {"field": "a"}}]})
    with pytest.raises(playbooks.PlaybookError, match="unknown param"):
        playbooks.parse_playbook({"name": "x", "steps": [{"check": {"field": "{item}"}, "for_each": "nope"}]})
    with pytest.raises(playbooks.PlaybookError, match="steps"):
        playbooks.parse_playbook({"name": "x"})


def test_words_match_needs_all_words():
    p = playbooks.parse_playbook({"name": "x", "match": {"words": ["pizza", "order"]}, "steps": [{"open": "u"}]})
    assert p.matches("Please ORDER a Pizza") and not p.matches("a pizza please")


def test_elements_are_found_by_label_and_kind():
    v = PageView(url="u", title="t", elements=[
        Element(id="e1", tag="input", type="text", name="Customer name:"),
        Element(id="e2", tag="input", type="radio", name="Medium"),
        Element(id="e3", tag="input", type="checkbox", name="Mushroom"),
        Element(id="e4", tag="input", type="password", name="Password"),
        Element(id="e5", tag="button", name="Submit order"),
        Element(id="e6", tag="select", name="Dropdown (select) Open this select menu One Two")])
    assert playbooks.find_element(v, "customer name", "type").id == "e1"
    assert playbooks.find_element(v, "mushrooms", "check").id == "e3"          # the label is inside the value
    assert playbooks.find_element(v, "medium", "check").id == "e2"
    assert playbooks.find_element(v, "Dropdown (select)", "select").id == "e6"
    assert playbooks.find_element(v, "Password", "type") is None                # never a password field
    assert playbooks.find_element(v, "Submit order", "type") is None            # kinds do not mix


# -- running through the agent ------------------------------------------------------------------------------

def test_playbook_runs_with_the_approval_gate_and_leaves_a_trace(isolated):
    write(isolated, BOOK)
    asked, seen = [], []
    rec = run([[call("run_playbook", playbook="test-pizza",
                     params={"custname": "Test User", "size": "medium", "toppings": ["mushrooms"]})],
               [call("finish", report="[done] ordered a medium pizza")]],
              ask=lambda s: asked.append(s) or True, seen=seen)
    hint = seen[0][2]
    assert hint["role"] == "user" and "test-pizza" in hint["content"] and "run_playbook" in hint["content"]
    assert asked == ["Submit the pizza order for Test User"]
    assert [s.tool for s in rec.steps] == ["request_approval", "run_playbook", "finish"]
    out = tool_results(rec)[0]
    assert out["next"] == "Report what the site returned." and "Order received" in out["page"]["title"]
    sent = out["page"]["text"]
    assert "Test User" in sent and "medium" in sent and "mushroom" in sent
    assert any("skipped: no value" in line for line in out["log"])                # phone was not given
    assert rec.state["playbooks"][0]["name"] == "test-pizza"
    trace = yaml.safe_load((rec.dir / "trace.yaml").read_text())
    kinds = [next(iter(s)) for s in trace["steps"]]
    assert kinds == ["open", "type", "check", "check", "submit"]
    assert trace["steps"][-1]["submit"]["element"] == "Submit order"
    assert playbooks.parse_playbook(trace).matches("go to forms.test/pizza")      # a trace is a valid playbook


def test_denied_approval_stops_the_playbook_without_submitting(isolated):
    write(isolated, BOOK)
    rec = run([[call("run_playbook", playbook="test-pizza", params={"custname": "Test User", "size": "medium"})],
               [call("finish", report="[not done] approval denied")]], ask=lambda s: False)
    out = tool_results(rec)[0]
    assert out["failed_step"]["error"].startswith("the approval was denied")
    assert "Order received" not in json.dumps(out)


def test_a_failed_step_hands_the_page_back_to_the_model(isolated):
    book = dict(BOOK, steps=[BOOK["steps"][0], {"type": {"field": "Delivery address", "text": "x"}}, *BOOK["steps"][1:]])
    write(isolated, book)
    rec = run([[call("run_playbook", playbook="test-pizza", params={"custname": "Test User", "size": "medium"})],
               [call("finish", report="[partial]")]])
    out = tool_results(rec)[0]
    assert out["completed"] == 1 and out["failed_step"]["n"] == 2
    assert "Delivery address" in out["failed_step"]["error"] and "Continue by hand" in out["next"]
    assert out["steps_left"][0] == book["steps"][1] and any('"Customer name' in e for e in out["page"]["elements"])
    assert rec.state["playbooks"][0]["failed_step"]["n"] == 2


def test_password_steps_are_refused(isolated):
    write(isolated, dict(BOOK, name="pw", steps=[BOOK["steps"][0], {"type": {"field": "Secret", "text": "hunter2"}}]))
    rec = run([[call("run_playbook", playbook="pw", params={"custname": "x", "size": "small"})],
               [call("finish", report="[not done]")]])
    out = tool_results(rec)[0]
    assert "no type element" in out["failed_step"]["error"]
    assert out["completed"] == 1 and not any(l.startswith("2.") for l in out["log"])   # nothing was typed


def test_missing_params_and_unknown_names_are_explained(isolated):
    write(isolated, BOOK)
    rec = run([[call("run_playbook", playbook="test-pizza", params={"size": "medium"})],
               [call("run_playbook", playbook="nope", params={})],
               [call("finish", report="[not done]")]])
    first, second = tool_results(rec)[:2]
    assert "custname" in first["error"] and "params" in first
    assert "no playbook named 'nope'" in second["error"] and "test-pizza" in second["error"]


def test_no_hint_without_a_matching_playbook(isolated):
    write(isolated, BOOK)
    seen = []
    run([[call("finish", report="[done]")]], objective="Find a paper on ammonia.", seen=seen)
    assert not any("[Scout] A playbook" in (m.get("content") or "") for m in seen[0])
