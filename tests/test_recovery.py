"""Fixes from the first resilience eval run (2026-11-17): crash resume with half-run turns (r13), a PDF found
after its paper was saved (r02), a dead link with only a title to go on (r11), the verifier's truncated JSON,
and the task-record line on every report."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from scout import verify
from scout.agent import Scout
from scout.library import Item, _read_index, library_dir, read_front_matter, save_item
from scout.resilience import RetryPolicy
from scout.task import TaskRecord
from test_phase1 import call, handler, make_pdf


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOUT_LIBRARY_DIR", str(tmp_path / "library"))
    monkeypatch.setenv("SCOUT_TASKS_DIR", str(tmp_path / "tasks"))


class Fake:
    """Scripted planner; a turn may be a function of the transcript (to use paths a tool returned)."""

    def __init__(self, script):
        self.script = list(script)
        self.seen: list[list[dict]] = []

    def chat(self, role, messages, **kw):
        self.seen.append([dict(m) for m in messages])
        turn = self.script.pop(0) if self.script else "…"
        if callable(turn):
            turn = turn(messages)
        msg = (SimpleNamespace(content="", tool_calls=turn) if isinstance(turn, list)
               else SimpleNamespace(content=turn, tool_calls=None))
        return SimpleNamespace(message=msg, text=msg.content or "", tool_calls=list(msg.tool_calls or []),
                               raw=SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop")]))


def client_for(h):
    @contextmanager
    def client(tor=False):
        with httpx.Client(transport=httpx.MockTransport(h), follow_redirects=True) as c:
            yield c
    return client


def scout(rec, llm, h=handler, **kw):
    return Scout(rec, llm=llm, client_factory=client_for(h), verify=False,
                 model_policy=RetryPolicy(attempts=2, base=0, sleep=lambda s: None), **kw)


def save(title, doi, **kw):
    return call("save_to_library", title=title, kind="paper", doi=doi, year=2024, source="openalex", **kw)


# -- R5: crash in the middle of a turn ---------------------------------------------------------------

def test_resume_repairs_a_half_run_turn():
    class Crash(Exception):
        pass

    def crash_after_first(msg):
        if msg.startswith("[1] "):
            raise Crash
    turn = [save("Paper A", "10.1/a"), save("Paper B", "10.1/b"), save("Paper C", "10.1/c")]
    rec = TaskRecord.create("Save 3 papers on ammonia.", tor=False)
    with pytest.raises(Crash):
        scout(rec, Fake([turn]), notify=crash_after_first).run()

    rec = TaskRecord.load(rec.id)                                   # resume from task.json alone
    assert len(rec.steps) == 1 and rec.steps[0].call_id == turn[0].id
    llm = Fake([[save("Paper B", "10.1/b"), save("Paper C", "10.1/c")], [call("finish", report="[done] 3 saved")]])
    rec = scout(rec, llm).run()

    first = llm.seen[0]
    i = next(k for k, m in enumerate(first) if m.get("tool_calls"))
    results = {m["tool_call_id"]: m["content"] for m in first[i + 1:i + 4]}
    assert set(results) == {c.id for c in turn}                     # every call answered, right after its turn
    assert results[turn[0].id].startswith("(recovered after an interruption)")
    assert results[turn[1].id].startswith("NOT RUN") and results[turn[2].id].startswith("NOT RUN")
    assert rec.status == "done" and len(rec.files) == 3
    assert "_Task record: 3 items saved to the library by this task, 0 with a verified file._" in rec.report


def test_report_carries_the_record_line_even_when_it_claims_more():
    rec = scout(TaskRecord.create("Save 4 papers.", tor=False),
                Fake([[save("Only one", "10.1/one")], [call("finish", report="[done] 4 papers saved")]])).run()
    assert rec.report.endswith("_Task record: 1 item saved to the library by this task, 0 with a verified file._")


# -- PDF found after the paper was saved --------------------------------------------------------------

def test_file_is_attached_to_an_item_saved_without_one():
    def save_with_file(messages):
        path = next(json.loads(m["content"])["path"] for m in reversed(messages)
                    if m["role"] == "tool" and '"path"' in m["content"])
        return [call("save_to_library", title="Attention Is All You Need", kind="paper", arxiv="1706.03762",
                     source="arxiv", download_path=path)]
    rec = scout(TaskRecord.create("Save arXiv 1706.03762 with its PDF.", tor=False), Fake([
        [call("save_to_library", title="Attention Is All You Need", kind="paper", arxiv="1706.03762", source="arxiv",
              source_url="https://arxiv.org/abs/1706.03762")],
        [call("download_file", url="https://arxiv.org/pdf/1706.03762", expected_title="Attention Is All You Need",
              expected_kind="pdf")],
        save_with_file,
        [call("finish", report="[done]")]])).run()
    attach = rec.steps[2]
    assert attach.ok and '"file_attached": true' in attach.summary
    [entry] = _read_index(library_dir())
    meta = read_front_matter(library_dir() / entry["path"])
    assert entry["file"] and meta["file"] == entry["file"] and meta["sha256"] == entry["sha256"]
    assert (library_dir() / entry["path"]).parent.joinpath(meta["file"]).exists()
    assert "1 item saved to the library by this task, 1 with a verified file" in rec.report


def test_library_attach_then_same_file_again_is_a_duplicate(tmp_path):
    md, created = save_item(Item(title="P", doi="10.1/p"), task_id="t1", body="notes")
    f = tmp_path / "p.pdf"
    f.write_bytes(make_pdf("P"))
    md2, created2 = save_item(Item(title="P", doi="10.1/p"), task_id="t1", file=f)
    assert md2 == md and not created2 and read_front_matter(md)["file"] == "p.pdf"
    assert "notes" in md.read_text()                                    # the notes survive the attach
    md3, created3 = save_item(Item(title="P again", doi="10.1/other"), task_id="t2", file=f)
    assert md3 == md and not created3 and len(_read_index(library_dir())) == 1


def test_already_in_library_says_the_part_is_done():
    first = TaskRecord.create("seed", tor=False)
    f = first.downloads / "a.pdf"
    f.write_bytes(make_pdf("Attention Is All You Need"))
    save_item(Item(title="Attention", source_url="https://arxiv.org/pdf/1706.03762", arxiv="1706.03762"),
              task_id=first.id, file=f)
    llm = Fake([[call("download_file", url="https://arxiv.org/pdf/1706.03762")], [call("finish", report="[done]")]])
    scout(TaskRecord.create("Download arXiv 1706.03762.", tor=False), llm).run()
    result = json.loads(next(m["content"] for m in llm.seen[1] if m["role"] == "tool"))
    assert result["already_in_library"] and result["has_file"] and "objective is done" in result["note"]


# -- dead link, only the title known -------------------------------------------------------------------

def test_dead_link_falls_back_via_title_lookup():
    def h(request):
        u = request.url
        if u.path == "/missing.pdf":
            return httpx.Response(404, text="not found")
        if u.host == "api.openalex.org" and u.path == "/works":
            assert "title.search:Attention Is All You Need" in u.params["filter"]
            return httpx.Response(200, json={"results": [
                {"title": "Attention Is All You Need: a commentary", "locations": []},     # near miss: skipped
                {"title": "Attention Is All You Need", "doi": None, "best_oa_location": None,
                 "locations": [{"landing_page_url": "https://arxiv.org/abs/1706.03762"}]}]})
        return handler(request)
    rec = scout(TaskRecord.create("Download it.", tor=False), Fake([
        [call("download_file", url="https://files.example/missing.pdf", expected_title="Attention Is All You Need",
              expected_kind="pdf")],
        [call("finish", report="[done]")]]), h=h).run()
    s = rec.steps[0]
    assert s.ok and "arxiv.org/pdf/1706.03762" in s.summary and "failed_first" in s.summary


# -- verifier -----------------------------------------------------------------------------------------

def test_truncated_checker_json_keeps_complete_claims():
    cut = ('{"claims": [\n{"claim": "Salt caverns are gas-tight.", "source": "S1", "verdict": "supported", '
           '"evidence": "gas-tight"},\n{"claim": "They store hydrogen in Texas.", "source": "S1", "verdict": '
           '"unsupported", "evidence": ""},\n{"claim": "Caverns cycle quic')
    claims = verify._parse_claims(cut)
    assert [c["verdict"] for c in claims] == ["supported", "unsupported"]
    with pytest.raises(ValueError):
        verify._parse_claims("no json at all")


def test_sources_include_catalogue_metadata():
    md, _ = save_item(Item(title="Ammonia cracking", doi="10.1/nh3", year=2023, authors=["A. B."],
                           extra={"cited_by": 338}), task_id="t", body="Cracking ammonia releases hydrogen.")
    [src] = verify.gather_sources([str(md)])
    assert src["text"].startswith("[catalogue record]") and "year: 2023" in src["text"] and "cited_by: 338" in src["text"]
    assert "Cracking ammonia" in src["text"]


# -- the think switch ---------------------------------------------------------------------------------

def test_think_switch_and_fallback_when_the_server_rejects_it():
    import openai
    from scout.config import load_settings
    from scout.llm import LLM

    sent = []

    def create(**kw):
        sent.append(kw)
        if "extra_body" in kw and len(sent) == 1 and reject:
            raise openai.BadRequestError("unknown field", response=httpx.Response(
                400, request=httpx.Request("POST", "http://x")), body=None)
        msg = SimpleNamespace(content="{}", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], usage=None)

    llm = LLM(load_settings())
    llm._resolved_model = "m"
    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    for reject in (False, True):
        sent.clear()
        assert llm.chat("extractor", [{"role": "user", "content": "q"}], max_tokens=512, think=False).text == "{}"
        assert sent[0]["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
        assert len(sent) == (2 if reject else 1)
        if reject:
            assert "extra_body" not in sent[-1]                        # the plain retry


# -- eval runner repeats ------------------------------------------------------------------------------

def test_runner_repeats_each_with_its_own_library(tmp_path, monkeypatch):
    import os
    import scout.llm
    from evals import phase_tasks as pt

    libs = []

    def fake_run_one(task, llm):
        libs.append((task["id"], os.environ["SCOUT_LIBRARY_DIR"]))
        return pt.Outcome(task["id"], task["phase"], status="done", passed=task["id"] != "r11-dead-pdf-link")
    monkeypatch.setattr(pt, "ROOT", tmp_path)
    monkeypatch.setattr(pt, "run_one", fake_run_one)
    monkeypatch.setattr(scout.llm, "LLM", lambda: SimpleNamespace(model_for=lambda role: "m"))
    code = pt.main(["--only", "r01-known-paper-pdf,r11-dead-pdf-link", "--repeats", "2"])
    assert code == 1                                                    # 50% < 75%
    assert [t for t, _ in libs] == ["r01-known-paper-pdf", "r11-dead-pdf-link"] * 2
    assert len({lib for _, lib in libs}) == 2 and libs[0][1].endswith("library-1") and libs[2][1].endswith("library-2")
    summary = next(tmp_path.glob("results/*-tasks/summary.md")).read_text()
    assert "| r01-known-paper-pdf | 2/2 |" in summary and "| r11-dead-pdf-link | 0/2 |" in summary
    assert "r01-known-paper-pdf (#2)" in summary


def test_failed_download_without_a_title_says_to_retry_with_one():
    def h(request):
        if request.url.path == "/missing.pdf":
            return httpx.Response(404, text="not found")
        return handler(request)
    llm = Fake([[call("download_file", url="https://files.example/missing.pdf", expected_kind="pdf")],
                [call("finish", report="[not done]")], [call("finish", report="[not done] dead link")]])
    scout(TaskRecord.create("Download it.", tor=False), llm, h=h).run()
    result = json.loads(next(m["content"] for m in llm.seen[1] if m["role"] == "tool"))
    assert not result["verified"] and "expected_title" in result["next"]


def test_text_files_keep_their_extension_and_small_any_files_pass(tmp_path):
    from scout.tools import web
    assert web.safe_name("file_1.txt", "csv") == "file_1.txt" and web.safe_name("data", "csv") == "data.csv"
    assert web.safe_name("report", "pdf") == "report.pdf"
    small = web.verify_bytes(b"Hello, World!\n", "https://x/f.txt", "", "f.txt", tmp_path, expected_kind="any")
    assert small.verified
    assert not web.verify_bytes(b"Not found\n", "https://x/f.pdf", "", "f.pdf", tmp_path, expected_kind="pdf").verified
    assert not web.verify_bytes(b"", "https://x/f", "", "f", tmp_path, expected_kind="any").verified
