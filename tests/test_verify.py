"""Claim verification at finish: supported claims pass, unsupported ones go back once, checker failures never block."""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest

from scout.agent import Scout
from scout.resilience import RetryPolicy
from scout.task import TaskRecord
from scout.verify import gather_sources, verify_report
from test_phase1 import call, handler


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOUT_LIBRARY_DIR", str(tmp_path / "library"))
    monkeypatch.setenv("SCOUT_TASKS_DIR", str(tmp_path / "tasks"))


def reply(turn):
    msg = (SimpleNamespace(content="", tool_calls=turn) if isinstance(turn, list)
           else SimpleNamespace(content=turn, tool_calls=None))
    return SimpleNamespace(message=msg, text=msg.content or "", tool_calls=list(msg.tool_calls or []),
                           raw=SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop")]))


class Fake:
    def __init__(self, planner, extractor):
        self.planner, self.extractor = list(planner), list(extractor)
        self.extractor_prompts: list[str] = []

    def chat(self, role, messages, **kw):
        if role == "extractor":
            self.extractor_prompts.append(messages[-1]["content"])
            out = self.extractor.pop(0)
            if isinstance(out, Exception):
                raise out
            return reply(json.dumps(out) if isinstance(out, dict) else out)
        return reply(self.planner.pop(0) if self.planner else "…")


def run(planner, extractor):
    @contextmanager
    def client(tor=False):
        with httpx.Client(transport=httpx.MockTransport(handler)) as c:
            yield c
    llm = Fake(planner, extractor)
    rec = Scout(TaskRecord.create("Summarise salt cavern storage.", tor=False), llm=llm, client_factory=client,
                model_policy=RetryPolicy(attempts=2, base=0, sleep=lambda s: None)).run()
    return rec, llm


SAVE = [call("save_to_library", title="Salt caverns primer", kind="page", source_url="https://site.example/a",
             notes="Salt caverns are gas-tight and cycle quickly. They store hydrogen in Texas and the UK.")]


def test_supported_report_is_accepted_with_a_note():
    rec, llm = run([SAVE, [call("finish", report="[done] Salt caverns are gas-tight (S1).")]],
                   [{"claims": [{"claim": "Salt caverns are gas-tight", "source": "S1", "verdict": "supported",
                                 "evidence": "Salt caverns are gas-tight"}]}])
    assert rec.status == "done" and "Verified: 1 claims checked" in rec.report
    assert "gas-tight and cycle quickly" in llm.extractor_prompts[0]          # the stored notes were the evidence


def test_unsupported_claim_goes_back_once_then_corrected_report_is_accepted():
    rec, _ = run([SAVE,
                  [call("finish", report="[done] Salt caverns hold 90% of Europe's hydrogen.")],
                  [call("finish", report="[done] Salt caverns are gas-tight and cycle quickly.")]],
                 [{"claims": [{"claim": "Salt caverns hold 90% of Europe's hydrogen", "source": "S1",
                               "verdict": "unsupported", "evidence": ""}]}])
    assert rec.status == "done" and "90%" not in rec.report
    tool_msgs = [m["content"] for m in rec.messages if m["role"] == "tool"]
    assert any("not supported by the sources" in t and "90%" in t for t in tool_msgs)
    assert [s.tool for s in rec.steps].count("verify_report") == 1             # checked once, not every finish


def test_checker_failure_never_blocks_finishing():
    rec, _ = run([SAVE, [call("finish", report="[done] summary")]], [RuntimeError("model hiccup")])
    assert rec.status == "done" and "Not verified" in rec.report


def test_no_saved_items_means_no_verification():
    rec, llm = run([[call("finish", report="[not done] nothing found")]], [])
    assert rec.status == "done" and llm.extractor_prompts == []


def test_sources_include_pdf_text(tmp_path):
    from scout.library import Item, save_item
    from test_phase1 import make_pdf
    f = tmp_path / "p.pdf"
    f.write_bytes(make_pdf("Underground Hydrogen Storage Capacity"))
    md, _ = save_item(Item(title="UHS", doi="10.1/uhs"), task_id="t", body="Notes here.", file=f)
    [src] = gather_sources([str(md)])
    assert src["id"] == "S1" and "Notes here." in src["text"] and "Underground Hydrogen Storage" in src["text"]


def test_json_with_thinking_and_prose_is_parsed():
    llm = SimpleNamespace(chat=lambda *a, **k: reply('<think>hmm</think>Here: {"claims": [{"claim": "x", "verdict": "partial"}]}'))
    from scout.library import Item, save_item
    md, _ = save_item(Item(title="A", doi="10.1/a"), task_id="t", body="x")
    v = verify_report(llm, "x", [str(md)])
    assert v.checked == 1 and len(v.partial) == 1 and v.ok
