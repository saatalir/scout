"""Rigor thorough (docs/RIGOR.md RG3/RG4): rules checked in code before finishing; counter-searches; budgets."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest

from scout import sufficiency
from scout.agent import MAX_STEPS, Scout, THOROUGH_STEPS
from scout.resilience import RetryPolicy
from scout.task import TaskRecord
from test_phase1 import call
from test_rigor import article
from test_verify import Fake


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOUT_LIBRARY_DIR", str(tmp_path / "library"))
    monkeypatch.setenv("SCOUT_TASKS_DIR", str(tmp_path / "tasks"))


SITES = ["alpha", "beta", "gamma", "delta", "epsilon"]
PAGES = {f"{s}.example": article(f"Page {s}", [f"Site {s} says salt caverns cycle hydrogen quickly, note {i} {s}."
                                              for i in range(12)]) for s in SITES}


def handler(request: httpx.Request) -> httpx.Response:
    page = PAGES.get(request.url.host)
    return (httpx.Response(200, text=page, headers={"content-type": "text/html"}) if page
            else httpx.Response(404, text="not found"))


def fake_search(hosts_by_query: dict[str, list[str]]):
    async def search(query, want=8, tor=False):
        hosts = hosts_by_query.get(query, [])
        return SimpleNamespace(results=[SimpleNamespace(title=h, url=f"https://{h}/x", snippet="…") for h in hosts],
                               tor=tor, needs_human=False, live_view=None)
    return search


SEARCHES = {"caverns": ["alpha.example", "beta.example"], "caverns criticism": ["gamma.example"],
            "caverns 2": ["alpha.example"], "caverns 3": ["beta.example"]}
READ_FIVE = [[call("fetch_page", url=f"https://{s}.example/")] for s in SITES]
SEARCH_ALL = [[call("web_search", query="caverns")], [call("web_search", query="caverns criticism", purpose="counter")],
              [call("web_search", query="caverns 2")], [call("web_search", query="caverns 3")]]
CLAIMS = {"claims": ["Salt caverns cycle hydrogen quickly."]}


def run(planner, extractor, rigor="thorough", **kw):
    @contextmanager
    def client(tor=False):
        with httpx.Client(transport=httpx.MockTransport(handler)) as c:
            yield c
    llm = Fake(planner, extractor)
    rec = TaskRecord.create("What is known about hydrogen in salt caverns?", tor=False)
    rec.state["rigor"] = rigor
    scout = Scout(rec, llm=llm, client_factory=client, web_search_fn=fake_search(SEARCHES),
                  model_policy=RetryPolicy(attempts=2, base=0, sleep=lambda s: None), **kw)
    return scout.run(), llm, scout


def tool_texts(rec) -> list[str]:
    return [m.get("content") or "" for m in rec.messages if m.get("role") == "tool"]


# -- the rules on their own -----------------------------------------------------------------------------

def test_log_search_counts_what_is_new():
    st: dict = {}
    r = {"results": [{"url": "https://www.a.example/1"}, {"url": "https://b.example/2"}, {"url": "https://a.example/3"}]}
    assert sufficiency.log_search(st, "web_search", "q", "main", r)["new"] == 2      # a.example once
    assert sufficiency.log_search(st, "web_search", "q2", "counter", r)["new"] == 0
    papers = {"results": [{"doi": "10.1/X"}, {"title": "A Paper"}]}
    assert sufficiency.log_search(st, "search_papers", "q3", "bogus", papers) == {
        "tool": "search_papers", "query": "q3", "purpose": "main", "results": 2, "new": 2}


def test_process_rules():
    st: dict = {}
    names = [m.split(":")[0] for m in sufficiency.process_misses(st, sites=2)]
    assert names == ["Breadth", "Disagreement", "Saturation"]
    st["searches"] = [{"purpose": "counter", "new": 3}, {"purpose": "main", "new": 0}, {"purpose": "main", "new": 0}]
    assert sufficiency.process_misses(st, sites=5) == []
    st["searches"].append({"purpose": "main", "new": 1})                            # still finding new material
    assert [m.split(":")[0] for m in sufficiency.process_misses(st, sites=5)] == ["Saturation"]


def test_corroboration_rule_lists_single_source_findings():
    claims = [{"claim": "two", "verdict": "supported", "independent": 2},
              {"claim": "one", "verdict": "supported", "independent": 1},
              {"claim": "none", "verdict": "unsupported", "independent": 0}]
    (miss,) = sufficiency.corroboration_misses(claims)
    assert "1 finding(s) rest on a single site" in miss and "- one" in miss and "none" not in miss
    assert sufficiency.corroboration_misses(claims[:1]) == []


# -- in the agent ---------------------------------------------------------------------------------------

def test_an_early_finish_is_sent_back_then_accepted_when_the_rules_are_met():
    judge = {"judgements": [{"c": 1, "supports": ["S1", "S2"]}]}
    rec, llm, _ = run([[call("finish", report="Caverns cycle quickly.")]] + READ_FIVE + SEARCH_ALL
                      + [[call("finish", report="Salt caverns cycle hydrogen quickly.")]], [CLAIMS, judge])
    assert rec.status == "done"
    sent_back = next(t for t in tool_texts(rec) if t.startswith("Not finished yet — rigor thorough"))
    assert "Breadth" in sent_back and "Disagreement" in sent_back and "Saturation" in sent_back
    assert "Corroboration" not in sent_back             # the checker is not run while cheap rules fail
    assert [s["purpose"] for s in rec.state["searches"]] == ["main", "counter", "main", "main"]
    assert [s["new"] for s in rec.state["searches"]] == [2, 1, 0, 0]
    assert "_Thoroughness rules: all met._" in rec.report
    assert "high (2 independent)" in rec.report
    assert len(llm.extractor_prompts) == 2             # checked once: the finish reuses the rule check's result


def test_single_source_findings_are_sent_back_at_most_twice():
    judge = {"judgements": [{"c": 1, "supports": ["S1"]}]}
    finish = [call("finish", report="Salt caverns cycle hydrogen quickly.")]
    rec, llm, _ = run(READ_FIVE + SEARCH_ALL + [finish, finish, finish], [CLAIMS, judge])
    assert rec.status == "done"
    backs = [t for t in tool_texts(rec) if t.startswith("Not finished yet — rigor thorough")]
    assert len(backs) == 2 and all("Corroboration" in b for b in backs)
    assert "Thoroughness rules not met: Corroboration" in rec.report
    assert "medium (1 independent)" in rec.report
    assert len(llm.extractor_prompts) == 2             # the same report is not checked again


def test_no_send_back_when_the_budget_is_nearly_used():
    rec, _, _ = run([[call("finish", report="Caverns cycle quickly.")]], [], max_steps=4)
    assert rec.status == "done"
    assert not any(t.startswith("Not finished yet — rigor thorough") for t in tool_texts(rec))
    assert "Thoroughness rules not met: Breadth, Disagreement, Saturation" in rec.report


def test_thorough_budgets_and_prompt():
    _, _, scout = run([[call("finish", report="x")]], [], max_steps=MAX_STEPS)
    assert scout.max_steps == THOROUGH_STEPS and scout.deadline.seconds == 90 * 60
    assert "Rigor: thorough" in scout.rec.messages[1]["content"]
    _, _, scout = run([[call("finish", report="x")]], [], max_steps=12)
    assert scout.max_steps == 12                       # an explicit budget is kept


def test_quick_and_standard_never_send_a_finish_back():
    for level in ("quick", "standard"):
        rec, _, scout = run([[call("web_search", query="caverns")], [call("finish", report="Caverns.")]], [],
                            rigor=level)
        assert rec.status == "done"
        assert not any(t.startswith("Not finished yet — rigor thorough") for t in tool_texts(rec))
        assert "Thoroughness rules" not in rec.report
        assert ("searches" in rec.state) == (level == "standard")
        assert ("Rigor:" in scout.rec.messages[1]["content"]) == (level == "standard")
