"""Rigor (docs/RIGOR.md): the evidence store (RG1), corroboration (RG2), computed confidence (RG5) and the setting."""

from __future__ import annotations

import json
from contextlib import contextmanager

import httpx
import pytest

from scout import evidence, verify
from scout.agent import Scout
from scout.resilience import RetryPolicy
from scout.task import TaskRecord
from test_phase1 import call
from test_verify import Fake


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOUT_LIBRARY_DIR", str(tmp_path / "library"))
    monkeypatch.setenv("SCOUT_TASKS_DIR", str(tmp_path / "tasks"))


def article(title: str, sentences: list[str]) -> str:
    body = "".join(f"<p>{s}</p>" for s in sentences)
    return f"<!doctype html><html><head><title>{title}</title></head><body><article><h1>{title}</h1>{body}</article></body></html>"


CAVERNS = [f"Salt caverns are gas-tight and cycle hydrogen quickly, report number {i} says." for i in range(12)]
PRESS = [f"The operator announced that cavern {i} in Texas holds hydrogen at high pressure since this year." for i in range(12)]
REVIEW = [f"An independent review found that salt caverns cycle hydrogen quickly in study {i}." for i in range(12)]

PAGES = {
    ("news.alpha.example", "/caverns"): article("Caverns explained", CAVERNS),
    ("www.beta.example", "/review"): article("A review", REVIEW),
    ("gamma.example", "/press"): article("Press release", PRESS),
    # gamma's release reposted with its own header: a different text (kept), but a near-copy (not independent)
    ("delta.example", "/press-copy"): article("Reposted", ["Reposted from the operator's newsroom today."] + PRESS),
}


def handler(request: httpx.Request) -> httpx.Response:
    page = PAGES.get((request.url.host, request.url.path))
    if page is None:
        return httpx.Response(404, text="not found")
    return httpx.Response(200, text=page, headers={"content-type": "text/html; charset=utf-8"})


def run(planner, extractor, rigor="standard", objective="What is known about hydrogen in salt caverns?"):
    @contextmanager
    def client(tor=False):
        with httpx.Client(transport=httpx.MockTransport(handler)) as c:
            yield c
    llm = Fake(planner, extractor)
    rec = TaskRecord.create(objective, tor=False)
    rec.state["rigor"] = rigor
    rec = Scout(rec, llm=llm, client_factory=client,
                model_policy=RetryPolicy(attempts=2, base=0, sleep=lambda s: None)).run()
    return rec, llm


FETCH_ALL = [[call("fetch_page", url="https://news.alpha.example/caverns")],
             [call("fetch_page", url="https://www.beta.example/review")],
             [call("fetch_page", url="https://gamma.example/press")],
             [call("fetch_page", url="https://delta.example/press-copy")]]

CLAIMS = {"claims": ["Salt caverns cycle hydrogen quickly.", "A Texas cavern holds hydrogen at high pressure.",
                     "Salt caverns are gas-tight."]}
JUDGE = {"judgements": [{"c": 1, "supports": ["S1", "S2"], "partial": [], "contradicts": []},
                        {"c": 2, "supports": ["S3", "S4"], "partial": [], "contradicts": []},
                        {"c": 3, "supports": ["S1"], "partial": [], "contradicts": ["S2"]}]}


# -- RG1: the evidence store ---------------------------------------------------------------------------

def test_quick_keeps_no_evidence_and_uses_the_old_check():
    rec, llm = run(FETCH_ALL + [[call("finish", report="Caverns cycle quickly.")]], [], rigor="quick")
    assert rec.status == "done"
    assert not rec.state.get("evidence")
    assert not (rec.dir / "evidence").exists()
    assert llm.extractor_prompts == []            # quick verifies library items only: none were saved


def test_standard_keeps_each_page_read_once(tmp_path):
    rec, _ = run(FETCH_ALL + [[call("fetch_page", url="https://gamma.example/press")]]
                 + [[call("finish", report="Caverns cycle quickly.")]],
                 [CLAIMS, JUDGE])
    items = rec.state["evidence"]
    # the second fetch of gamma's page has the same text: one entry
    assert [i["url"] for i in items] == ["https://news.alpha.example/caverns", "https://www.beta.example/review",
                                         "https://gamma.example/press", "https://delta.example/press-copy"]
    assert all(i["via"] == "fetch_page" for i in items)
    first = evidence.text_of(items[0])
    assert "gas-tight" in first and not first.startswith("---")
    again = TaskRecord.load(rec.id)                 # the index is in the record: a resumed task keeps it
    assert again.state["evidence"] == items


def test_short_texts_are_not_evidence(tmp_path):
    state: dict = {}
    assert evidence.add(state, tmp_path, url="u", title="t", text="too short", via="fetch_page") is None
    assert state.get("evidence", []) == []


def test_merge_adds_only_new_texts(tmp_path):
    parent: dict = {}
    child: dict = {}
    a = evidence.add(parent, tmp_path / "p", url="https://a.example", title="A", text="a" * 300, via="fetch_page")
    evidence.add(child, tmp_path / "c", url="https://a.example/copy", title="A", text="a" * 300, via="fetch_page")
    evidence.add(child, tmp_path / "c", url="https://b.example", title="B", text="b" * 300, via="browser_read")
    assert evidence.merge(parent, child["evidence"]) == 1
    assert [i["n"] for i in parent["evidence"]] == [1, 2]
    assert parent["evidence"][0] == a


def test_rigor_levels_are_validated():
    assert evidence.normalize_rigor(None) == "quick"
    assert evidence.normalize_rigor(" Thorough ") == "thorough"
    with pytest.raises(ValueError, match="SCOUT_RIGOR"):
        evidence.normalize_rigor("deep")


# -- RG2 and RG5: corroboration and computed confidence -----------------------------------------------

def test_corroboration_counts_independent_sources_and_builds_the_table():
    report = "Salt caverns cycle hydrogen quickly. A Texas cavern holds hydrogen. Caverns are gas-tight."
    rec, llm = run(FETCH_ALL + [[call("finish", report=report)]], [CLAIMS, JUDGE])
    assert rec.status == "done"
    claims = {c["claim"]: c for c in rec.state["corroboration"]["claims"]}
    quick = claims["Salt caverns cycle hydrogen quickly."]
    assert quick["independent"] == 2 and quick["confidence"] == "high"        # two sites, different texts
    texas = claims["A Texas cavern holds hydrogen at high pressure."]
    assert texas["supports"] == ["S3", "S4"] and texas["independent"] == 1  # the repost is not a second source
    assert texas["confidence"] == "medium"
    tight = claims["Salt caverns are gas-tight."]
    assert tight["confidence"] == "low" and tight["contradicts"] == ["S2"]   # supported once, contradicted once
    assert "## Findings and confidence" in rec.report
    assert "high (2 independent)" in rec.report and "medium (1 independent)" in rec.report
    assert "S2 beta.example" in rec.report                                    # sources named by site
    assert "rest on a single source" in rec.report and "contradicted by at least one source" in rec.report
    assert len(llm.extractor_prompts) == 2                                    # list the claims, then one batch


def test_unsupported_claims_go_back_and_the_table_stays():
    claims = {"claims": ["Salt caverns cycle hydrogen quickly.", "Caverns store helium on Mars."]}
    judge = {"judgements": [{"c": 1, "supports": ["S1", "S2"]}, {"c": 2, "supports": []}]}
    rec, llm = run(FETCH_ALL[:2] + [[call("finish", report="Caverns cycle quickly. Helium on Mars.")],
                                    [call("finish", report="Caverns cycle quickly.")]], [claims, judge])
    assert rec.status == "done"
    assert any("not supported by the sources" in (m.get("content") or "") for m in rec.messages)
    assert "Mars" not in rec.report.split("## Findings")[1]                   # unsupported claims are not in the table
    assert "high (2 independent)" in rec.report
    assert "not checked again" in rec.report
    assert len(llm.extractor_prompts) == 2                                    # checked once, not again


def test_sources_are_checked_in_batches(monkeypatch):
    monkeypatch.setattr(verify, "MAX_SOURCES", 2)
    judge1 = {"judgements": [{"c": 1, "supports": ["S1"]}]}
    judge2 = {"judgements": [{"c": 1, "supports": ["S4"]}, {"c": 1, "supports": ["S1"]}]}   # S1 is not in batch 2
    rec, llm = run(FETCH_ALL + [[call("finish", report="Caverns cycle quickly.")]],
                   [{"claims": ["Salt caverns cycle hydrogen quickly."]}, judge1, judge2])
    assert len(llm.extractor_prompts) == 3                                    # claims + two batches of two
    c = rec.state["corroboration"]["claims"][0]
    assert c["supports"] == ["S1", "S4"]           # S1 from batch 1, S4 from batch 2; batch 2's "S1" is ignored
    assert c["independent"] == 2
    assert "[S3]" in llm.extractor_prompts[2] and "[S1]" not in llm.extractor_prompts[2]


def test_a_failed_checker_never_blocks_finishing():
    rec, _ = run(FETCH_ALL[:1] + [[call("finish", report="Caverns cycle quickly.")]], [RuntimeError("down")])
    assert rec.status == "done"
    assert "Not verified against sources" in rec.report


@pytest.mark.parametrize("ind, part, conflict, want", [
    (2, 2, False, "high"), (3, 3, True, "low"), (1, 1, False, "medium"), (0, 2, False, "medium"),
    (0, 1, False, "low"), (0, 0, False, "unsupported"), (1, 1, True, "low")])
def test_confidence_is_computed_from_counts(ind, part, conflict, want):
    assert verify.confidence(ind, part, conflict) == want


def test_long_pages_show_their_relevant_part():
    filler = " ".join(["unrelated words about weather and sport"] * 400)
    text = filler + " Hydrogen cycling in salt caverns is fast according to the operator. " + filler
    out = verify._relevant_text(text, ["Hydrogen cycling in salt caverns is fast."], budget=1500)
    assert "salt caverns is fast" in out and len(out) <= 1500


# -- the setting -------------------------------------------------------------------------------------------

def test_cli_stores_the_level_and_resume_keeps_it(monkeypatch):
    import scout.agent
    import scout.cli

    seen = []

    class StubScout:
        def __init__(self, rec, **kw):
            seen.append(rec.state.get("rigor"))
            self.rec = rec

        def run(self):
            self.rec.status = "done"
            return self.rec

    monkeypatch.setattr(scout.agent, "Scout", StubScout)
    monkeypatch.setenv("SCOUT_RIGOR", "standard")
    assert scout.cli.main(["run", "--no-tor", "q"]) == 0
    assert scout.cli.main(["run", "--no-tor", "--rigor", "thorough", "q2"]) == 0
    tid = TaskRecord.list_ids()[-1]
    monkeypatch.setenv("SCOUT_RIGOR", "quick")                  # a resumed task keeps its own level
    assert scout.cli.main(["resume", tid]) == 0
    assert seen == ["standard", "thorough", "thorough"]
    monkeypatch.setenv("SCOUT_RIGOR", "deep")
    assert scout.cli.main(["run", "--no-tor", "q3"]) == 2


def test_a_budget_ended_report_is_checked_too():
    @contextmanager
    def client(tor=False):
        with httpx.Client(transport=httpx.MockTransport(handler)) as c:
            yield c
    llm = Fake(FETCH_ALL[:2] + [[call("finish", report="Caverns cycle quickly. Helium on Mars.")]],
               [{"claims": ["Salt caverns cycle hydrogen quickly.", "Caverns store helium on Mars."]},
                {"judgements": [{"c": 1, "supports": ["S1", "S2"]}, {"c": 2, "supports": []}]}])
    rec = TaskRecord.create("What is known about hydrogen in salt caverns?", tor=False)
    rec.state["rigor"] = "standard"
    rec = Scout(rec, llm=llm, client_factory=client, max_steps=2,
                model_policy=RetryPolicy(attempts=2, base=0, sleep=lambda s: None)).run()
    assert rec.status in ("partial", "failed")
    assert "high (2 independent)" in rec.report
    assert "Not supported by the sources read" in rec.report and "helium on Mars" in rec.report


def test_search_purpose_is_offered_only_above_quick():
    """G4: at quick the model sees exactly the tool schemas the gates G1-G3 measured (no counter-search hint)."""
    from scout.agent import TOOLS

    def purposes(rigor):
        rec = TaskRecord.create("q", tor=False)
        rec.state["rigor"] = rigor
        tools = Scout(rec, llm=Fake([], [])).tools
        return {t["function"]["name"] for t in tools if "purpose" in t["function"]["parameters"]["properties"]}

    assert purposes("quick") == set()
    assert purposes("standard") == purposes("thorough") == {"web_search", "search_papers"}
    assert any("purpose" in t["function"]["parameters"]["properties"] for t in TOOLS)   # the shared list is untouched
