"""Agent-loop resilience (docs/ROADMAP.md §1): every 'try hard' behaviour, offline.

Fake model, mock HTTP. Each test forces one failure mode and checks Scout's response to it.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import openai
import pytest

from scout.agent import Scout
from scout.resilience import RetryPolicy
from scout.task import TaskRecord
from test_phase1 import call, handler, make_pdf, mock_client  # noqa: F401  (isolated_dirs fixture is autouse there)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOUT_LIBRARY_DIR", str(tmp_path / "library"))
    monkeypatch.setenv("SCOUT_TASKS_DIR", str(tmp_path / "tasks"))


class Fake:
    """Scripted planner. Entries: list of tool calls, a text reply, an Exception to raise, or ('cut', text)
    for an answer truncated by the length limit."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

    def chat(self, role, messages, **kw):
        self.calls.append({"messages": [dict(m) for m in messages], **{k: v for k, v in kw.items() if k != "tools"},
                           "tools": [t["function"]["name"] for t in kw.get("tools") or []]})
        turn = self.script.pop(0) if self.script else "…"
        if isinstance(turn, Exception):
            raise turn
        finish = "stop"
        if isinstance(turn, tuple) and turn[0] == "cut":
            turn, finish = turn[1], "length"
        msg = (SimpleNamespace(content="", tool_calls=turn) if isinstance(turn, list)
               else SimpleNamespace(content=turn, tool_calls=None))
        raw = SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish)])
        return SimpleNamespace(message=msg, text=msg.content or "", tool_calls=list(msg.tool_calls or []), raw=raw)


def fast_policy() -> RetryPolicy:
    return RetryPolicy(attempts=3, base=0, cap=0, max_elapsed=10, sleep=lambda s: None)


def run(script, *, transport_handler=handler, objective="Find papers on hydrogen.", **kw):
    @contextmanager
    def client(tor=False):
        with httpx.Client(transport=httpx.MockTransport(transport_handler), follow_redirects=True) as c:
            yield c
    llm = Fake(script)
    rec = Scout(TaskRecord.create(objective, tor=False), llm=llm, client_factory=client,
                model_policy=kw.pop("model_policy", fast_policy()), **kw).run()
    return rec, llm


def user_nudges(llm: Fake) -> list[str]:
    last = llm.calls[-1]["messages"]
    return [m["content"] for m in last if m["role"] == "user"][1:]


# -- R4: stuck detection ---------------------------------------------------------------------------------

def test_third_identical_call_is_refused():
    same = lambda: [call("search_papers", query="hydrogen", source="crossref")]   # noqa: E731
    rec, _ = run([same(), same(), same(), [call("finish", report="[done] searched")]])
    assert [s.ok for s in rec.steps[:3]] == [True, True, False]
    assert "already made this exact" in rec.steps[2].summary


def test_failure_streak_triggers_reflection():
    bad = lambda i: [call("fetch_page", url=f"https://site.example/missing{i}")]   # noqa: E731  (404s)
    rec, llm = run([bad(1), bad(2), bad(3), [call("finish", report="[not done] pages missing")],
                    [call("finish", report="[not done] tried 3 URLs")]])
    assert any("Reflection: the last steps failed" in n for n in user_nudges(llm))


def test_no_progress_window_triggers_reflection(monkeypatch):
    import scout.agent as agent
    monkeypatch.setattr(agent, "STUCK_WINDOW", 2)
    rec, llm = run([[call("search_papers", query=f"q{i}", source="crossref")] for i in range(3)]
                   + [[call("finish", report="[done]")]])
    assert any("no new items for a while" in n for n in user_nudges(llm))


# -- R7: honest completion -----------------------------------------------------------------------------------

def test_finish_with_unaddressed_failures_is_challenged_once():
    rec, llm = run([[call("fetch_page", url="https://site.example/missing")],
                    [call("finish", report="Done.")],
                    [call("finish", report="[not done] the page is missing; tried it once")]])
    assert rec.status == "done" and rec.state["finish_challenged"]
    tool_msgs = [m["content"] for m in llm.calls[-1]["messages"] if m["role"] == "tool"]
    assert any(t.startswith("Not finished yet") for t in tool_msgs)


def test_clean_finish_is_not_challenged():
    rec, _ = run([[call("search_papers", query="hydrogen", source="crossref")], [call("finish", report="[done]")]])
    assert rec.status == "done" and not rec.state["finish_challenged"]


# -- R3: budgets ---------------------------------------------------------------------------------------------------

def test_step_budget_ends_with_a_final_report_and_partial_status():
    save = [call("save_to_library", title="Hydrogen paper", kind="paper", doi="10.1/abc")]
    searches = [[call("search_papers", query=f"q{i}", source="crossref")] for i in range(3)]
    final = [call("finish", report="[partial] saved 1 of 5 papers")]
    rec, llm = run([save] + searches + [final], max_steps=4)
    assert rec.status == "partial" and "[partial]" in rec.report
    assert llm.calls[-1]["tools"] == ["finish"]                   # the last turn could only report
    assert any("Budget almost used" in n for n in user_nudges(llm))


def test_time_budget_expiry_ends_with_report():
    rec, llm = run([[call("finish", report="[not done] out of time")]], task_minutes=1e-6)
    assert llm.calls[0]["tools"] == ["finish"]
    assert rec.status == "failed" and "out of time" in rec.report
    assert len(rec.steps) == 0                                    # no tool ran after the budget expired


def test_slow_tool_is_cut_off(monkeypatch):
    import scout.agent as agent
    monkeypatch.setattr(agent.Scout, "_search_papers", lambda self, c, a: time.sleep(2) or {"count": 0})
    rec, _ = run([[call("search_papers", query="q", source="crossref")], [call("finish", report="[not done]")],
                  [call("finish", report="[not done] search too slow")]], tool_seconds=0.1)
    assert rec.steps[0].ok is False and "did not finish within" in rec.steps[0].summary


# -- R1/R6: model failures ---------------------------------------------------------------------------------------------

def _conn_error():
    return openai.APIConnectionError(request=httpx.Request("POST", "https://strata/v1/chat/completions"))


def test_transient_model_errors_are_retried():
    rec, llm = run([_conn_error(), _conn_error(), [call("finish", report="[done]")]])
    assert rec.status == "done" and len(llm.calls) == 3


def test_model_outage_pauses_task_as_waiting_and_resume_continues():
    rec, _ = run([_conn_error()] * 3)
    assert rec.status == "waiting" and f"scout resume {rec.id}" in rec.report
    # resume from the saved record, model back
    again = Scout(TaskRecord.load(rec.id), llm=Fake([[call("finish", report="[done] after resume")]]),
                  client_factory=lambda tor=False: mock_client(), model_policy=fast_policy()).run()
    assert again.status == "done" and "after resume" in again.report


def test_truncated_empty_answer_is_retried_with_bigger_budget():
    rec, llm = run([("cut", ""), [call("finish", report="[done]")]])
    assert rec.status == "done"
    assert [c["max_tokens"] for c in llm.calls] == [2048, 4096]


def test_permanent_model_error_is_not_swallowed():
    bad = openai.BadRequestError("bad", response=httpx.Response(400, request=httpx.Request("POST", "https://s")), body=None)
    with pytest.raises(openai.BadRequestError):
        run([bad])


# -- R2: fallbacks inside tools ------------------------------------------------------------------------------------------

def test_paper_search_falls_back_to_the_next_source():
    def h(request):
        if request.url.host == "api.openalex.org":
            return httpx.Response(503)
        return handler(request)
    rec, llm = run([[call("search_papers", query="hydrogen", source="openalex")], [call("finish", report="[done]")]],
                   transport_handler=h)
    s = rec.steps[0]
    assert s.ok and '"source": "crossref"' in s.summary and "openalex: HTTPStatusError" in s.summary


def test_paper_search_reports_when_every_source_fails():
    rec, _ = run([[call("search_papers", query="hydrogen", source="openalex")],
                  [call("finish", report="[not done]")], [call("finish", report="[not done] all sources down")]],
                 transport_handler=lambda r: httpx.Response(503))
    assert rec.steps[0].ok is False and "All sources failed" in rec.steps[0].summary


def test_pdf_download_falls_back_to_openalex_location():
    pdf = make_pdf("Hydrogen Blending Study")

    def h(request):
        u = request.url
        if u.path == "/paywalled.pdf":
            return httpx.Response(200, text="<!doctype html><html><title>Sign in</title>" + "x" * 400,
                                  headers={"content-type": "text/html"})
        if u.host == "api.openalex.org" and u.path.startswith("/works/doi:"):
            return httpx.Response(200, json={"best_oa_location": {"pdf_url": "https://repo.example/oa.pdf"},
                                             "locations": []})
        if u.path == "/oa.pdf":
            return httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"})
        return handler(request)
    rec, _ = run([[call("download_file", url="https://pub.example/paywalled.pdf", doi="10.1/h2",
                        expected_title="Hydrogen Blending Study", expected_kind="pdf")],
                  [call("finish", report="[done]")]], transport_handler=h)
    s = rec.steps[0]
    assert s.ok, s.summary
    assert "repo.example/oa.pdf" in s.summary and "failed_first" in s.summary
    assert list((rec.dir / "downloads").glob("*.html")) == []          # the unverified page was not kept


def test_pdf_found_via_landing_page_citation_meta():
    pdf = make_pdf("Seasonal Storage")

    def h(request):
        u = request.url
        if u.path == "/article/42":
            return httpx.Response(200, headers={"content-type": "text/html"},
                                  text='<html><head><meta name="citation_pdf_url" content="/files/42.pdf"></head></html>')
        if u.path == "/files/42.pdf":
            return httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"})
        return handler(request)
    rec, _ = run([[call("download_file", url="https://pub.example/article/42", expected_kind="pdf",
                        expected_title="Seasonal Storage")],
                  [call("finish", report="[done]")]], transport_handler=h)
    assert rec.steps[0].ok and "files/42.pdf" in rec.steps[0].summary


def test_state_survives_resume():
    rec, _ = run([[call("search_papers", query="hydrogen", source="crossref")]] + ["…"] * 3)
    loaded = TaskRecord.load(rec.id)
    assert loaded.state["calls"] and json.dumps(loaded.state)        # bookkeeping persisted in task.json


def test_dataset_search_falls_back_to_zenodo_when_a_portal_fails():
    def h(request):
        if request.url.host == "data.gov.ie":
            return httpx.Response(503)
        if request.url.host == "zenodo.org":
            return httpx.Response(200, json={"hits": {"hits": [{"id": 7, "doi": "10.5281/zenodo.7",
                "links": {"self_html": "https://zenodo.org/records/7"}, "metadata": {"title": "Air quality data",
                "publication_date": "2023-01-01"}, "files": [{"key": "aq.csv", "size": 900,
                "links": {"self": "https://zenodo.org/api/records/7/files/aq.csv/content"}}]}]}})
        return handler(request)
    rec, _ = run([[call("search_datasets", query="air quality", source="ireland")], [call("finish", report="[done]")]],
                 transport_handler=h)
    s = rec.steps[0]
    assert s.ok and '"source": "zenodo"' in s.summary and "ireland: HTTPStatusError" in s.summary


def test_new_paper_sources_are_dispatched():
    from scout.tools import academic
    seen = []

    def h(request):
        seen.append(request.url.host)
        if request.url.host == "api.semanticscholar.org":
            return httpx.Response(200, json={"data": [{"title": "S2 paper", "year": 2024, "externalIds": {"DOI": "10.1/s2"}}]})
        return httpx.Response(200, json={"esearchresult": {"idlist": []}})
    with httpx.Client(transport=httpx.MockTransport(h)) as c:
        assert academic.search_papers(c, "q", "semantic_scholar")[0].source == "semantic_scholar"
        assert academic.search_papers(c, "q", "pubmed") == []
    assert seen == ["api.semanticscholar.org", "eutils.ncbi.nlm.nih.gov"]
