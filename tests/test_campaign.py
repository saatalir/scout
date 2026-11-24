"""Campaigns (docs/RIGOR.md RG8/RG9): sessions with a time box, progress notes, compaction, end conditions,
and the unattended runs a schedule (RG10) starts."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import date

import httpx
import pytest

from scout import campaign
from scout.agent import Scout
from scout.resilience import RetryPolicy
from scout.task import TaskRecord
from test_phase1 import call
from test_rigor import FETCH_ALL, handler
from test_verify import Fake


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOUT_LIBRARY_DIR", str(tmp_path / "library"))
    monkeypatch.setenv("SCOUT_TASKS_DIR", str(tmp_path / "tasks"))


class Recording(Fake):
    """The fake model, keeping the messages each planner turn saw."""

    def __init__(self, planner, extractor=()):
        super().__init__(planner, list(extractor))
        self.seen: list[list[dict]] = []

    def chat(self, role, messages, **kw):
        if role != "extractor":
            self.seen.append([dict(m) for m in messages])
            out = self.planner.pop(0) if self.planner else "…"
            if isinstance(out, Exception):
                raise out
            from test_verify import reply
            return reply(out)
        return super().chat(role, messages, **kw)


@contextmanager
def client(tor=False):
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        yield c


def session(rec, planner, *, steps=2, minutes=60, extractor=()):
    llm = Recording(planner, extractor)
    rec = Scout(rec, llm=llm, client_factory=client, max_steps=steps, session_minutes=minutes,
                model_policy=RetryPolicy(attempts=1, base=0, sleep=lambda s: None)).run()
    return rec, llm


def new_task(**limits):
    rec = TaskRecord.create("What is known about hydrogen in salt caverns?", tor=False)
    rec.state["rigor"] = "standard"
    campaign.configure(rec.state, minutes=60, **limits)
    rec.save()
    return rec


NOTES = ("## Covered\nCaverns cycle quickly (alpha, beta).\n## Still needed\nThe Texas sites.\n"
         "## Open leads\nThe operator's newsroom.\n## Next searches\nTexas salt cavern hydrogen operator")


# -- sessions ------------------------------------------------------------------------------------------

def test_a_session_ends_paused_with_notes_and_a_digest():
    rec, _ = session(new_task(), FETCH_ALL[:2] + [[call("session_notes", notes=NOTES)]])
    assert rec.status == "paused"
    c = rec.state["campaign"]
    s = c["sessions"][0]
    assert s["ended_by"] == "the session's 2 steps are used"
    assert s["added"]["evidence"] == 2 and s["added"]["steps"] == 2
    assert c["notes"] == NOTES
    assert "**Session 1**" in rec.report and "2 new pages read" in rec.report and "Texas salt cavern" in rec.report
    assert f"resume {rec.id} --session 60m" in rec.report
    assert (rec.dir / "sessions" / "1.md").read_text().startswith("**Session 1**")


def test_the_next_session_starts_from_a_compacted_transcript():
    rec, _ = session(new_task(), FETCH_ALL[:2] + [[call("session_notes", notes=NOTES)]])
    old = list(rec.messages)
    rec, llm = session(TaskRecord.load(rec.id), FETCH_ALL[2:4] + [[call("finish", report="Caverns cycle quickly.")]],
                       steps=5, extractor=[{"claims": ["Salt caverns cycle hydrogen quickly."]},
                                           {"judgements": [{"c": 1, "supports": ["S1", "S2"]}]}])
    first = llm.seen[0]
    assert [m["role"] for m in first] == ["system", "user", "user"]           # system, objective, the compacted state
    state = first[2]["content"]
    assert "Session 2 of this research campaign" in state and NOTES in state
    assert "https://news.alpha.example/caverns" in state                        # pages already read are listed
    archived = json.loads((rec.dir / "sessions" / "transcript-1.json").read_text())
    assert archived == old                                                      # nothing lost
    assert rec.status == "done"
    s2 = rec.state["campaign"]["sessions"][1]
    assert s2["ended_by"] == "finish" and s2["added"]["evidence"] == 2
    assert len(rec.steps) == 5                                                  # 2 + 2 + finish: steps count per session


def test_two_sessions_that_find_nothing_end_the_campaign():
    rec = new_task()
    for _ in range(2):        # each session repeats a page already read: no new evidence, no new item
        rec, _ = session(TaskRecord.load(rec.id) if rec.messages else rec,
                         [[call("fetch_page", url="https://news.alpha.example/caverns")],
                          [call("session_notes", notes="nothing new")], [call("finish", report="Final report.")]],
                         steps=1)
    # session 1 read alpha (new); session 2 read it again (nothing new) — not yet two empty sessions
    assert rec.status == "paused"
    rec, _ = session(TaskRecord.load(rec.id),
                     [[call("fetch_page", url="https://news.alpha.example/caverns")],
                      [call("session_notes", notes="still nothing")], [call("finish", report="Final report.")]],
                     steps=1, extractor=[RuntimeError("checker down")])
    assert "found nothing new" in rec.state["campaign"]["ended"]
    assert rec.status == "partial" and rec.report.startswith("Final report.")


def test_max_sessions_ends_with_the_full_report():
    rec, _ = session(new_task(max_sessions=1), FETCH_ALL[:1] + [[call("session_notes", notes=NOTES)],
                                                                [call("finish", report="Full report.")]],
                     steps=1, extractor=[RuntimeError("checker down")])
    assert rec.state["campaign"]["ended"].startswith("the campaign's 1 session")
    assert rec.status == "partial" and rec.report.startswith("Full report.")


def test_a_campaign_past_its_end_date_only_writes_the_final_report():
    rec = new_task(until="2020-01-01")
    rec, llm = session(rec, [[call("finish", report="Final.")]])
    assert "end date (2020-01-01) has passed" in rec.state["campaign"]["ended"]
    assert rec.steps == [] and len(llm.seen) == 1 and rec.state["campaign"]["sessions"] == []


def test_an_interrupted_session_is_closed_and_notes_come_from_the_record():
    rec = new_task()
    campaign.start_session(rec.state, files=[], steps=0, minutes=60)       # a session whose process died
    rec.messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "o"}]
    rec.state.setdefault("searches", []).append({"tool": "web_search", "query": "caverns texas", "purpose": "main",
                                                 "results": 3, "new": 3})
    rec.save()
    rec, llm = session(rec, [[call("finish", report="Done.")]], extractor=[RuntimeError("down")])
    s1 = rec.state["campaign"]["sessions"][0]
    assert s1["ended_by"] == "interrupted"
    assert "caverns texas" in llm.seen[0][2]["content"]                      # fallback notes list the searches


def test_notes_fall_back_to_the_record_when_the_model_fails():
    rec, _ = session(new_task(), FETCH_ALL[:2] + [RuntimeError("model down")])
    assert rec.status == "paused"
    assert "from the record; the model wrote none" in rec.state["campaign"]["notes"]


def test_session_budgets_replace_the_task_budgets():
    rec = new_task()
    scout = Scout(rec, llm=Recording([]), client_factory=client, session_minutes=30)
    assert scout.deadline.seconds == 30 * 60
    rec.state["rigor"] = "thorough"
    assert Scout(rec, llm=Recording([]), client_factory=client, session_minutes=30).deadline.seconds == 30 * 60


# -- parsing and bookkeeping ----------------------------------------------------------------------------

@pytest.mark.parametrize("text, minutes", [("2h", 120), ("90m", 90), ("1h30m", 90), ("45", 45), (" 2H ", 120)])
def test_parse_duration(text, minutes):
    assert campaign.parse_duration(text) == minutes


@pytest.mark.parametrize("text", ["", "0", "-5m", "2x", "25h", "h"])
def test_parse_duration_rejects(text):
    with pytest.raises(ValueError, match="2h"):
        campaign.parse_duration(text)


def test_ended_reason_from_the_record():
    st: dict = {}
    campaign.configure(st, until="2026-12-31", max_sessions=3)
    assert campaign.ended_reason(st, today=date(2026, 12, 31)) is None
    assert "end date" in campaign.ended_reason(st, today=date(2027, 1, 1))
    with pytest.raises(ValueError):
        campaign.parse_until("next week")
    with pytest.raises(ValueError):
        campaign.configure(st, max_sessions=0)


def test_the_session_lock(tmp_path):
    assert campaign.acquire_lock(tmp_path)
    assert not campaign.acquire_lock(tmp_path)                 # this process holds it
    campaign.release_lock(tmp_path)
    (tmp_path / ".session.lock").write_text("999999999")       # a process that is gone: stale, taken over
    assert campaign.acquire_lock(tmp_path)
    campaign.release_lock(tmp_path)
    assert not (tmp_path / ".session.lock").exists()


# -- unattended runs (what a schedule starts) -----------------------------------------------------------

def test_blocked_and_preflight():
    rec = new_task()
    assert campaign.blocked_reason(rec) is None
    rec.status = "needs_human"
    assert "waits for a person" in campaign.blocked_reason(rec)
    rec.status = "done"
    assert "schedule --remove" in campaign.blocked_reason(rec)

    class Down:
        def list_models(self):
            raise ConnectionError("refused")

    class Up:
        def list_models(self):
            return ["m"]

    def host_down(tor):
        raise OSError("ssh timed out")

    assert "model server" in campaign.preflight(rec, llm_factory=Down, host_check=lambda tor: None)
    assert "browser host" in campaign.preflight(rec, llm_factory=Up, host_check=host_down)
    assert campaign.preflight(rec, llm_factory=Up, host_check=lambda tor: None) is None


def test_cli_unattended_skips_and_records_why(monkeypatch):
    import scout.agent
    import scout.cli

    ran = []

    class StubScout:
        def __init__(self, rec, **kw):
            ran.append(kw.get("session_minutes"))
            self.rec = rec

        def run(self):
            self.rec.status = "paused"
            return self.rec

    monkeypatch.setattr(scout.agent, "Scout", StubScout)
    rec = new_task()
    monkeypatch.setattr(campaign, "preflight", lambda rec: "the model server did not answer (x)")
    assert scout.cli.main(["resume", rec.id, "--session", "30m", "--unattended"]) == 0
    assert ran == []
    skipped = TaskRecord.load(rec.id).state["campaign"]["skipped"]
    assert skipped[-1]["reason"].startswith("the model server")
    assert not (rec.dir / ".session.lock").exists()             # released after a failed preflight

    monkeypatch.setattr(campaign, "preflight", lambda rec: None)
    assert scout.cli.main(["resume", rec.id, "--unattended"]) == 0     # the stored session length is used
    assert ran == [30]
    assert not (rec.dir / ".session.lock").exists()

    (rec.dir / ".session.lock").write_text(str(os.getpid()))          # a session is running
    assert scout.cli.main(["resume", rec.id, "--unattended"]) == 0
    assert ran == [30]
    assert "already" in TaskRecord.load(rec.id).state["campaign"]["skipped"][-1]["reason"] or \
        "running" in TaskRecord.load(rec.id).state["campaign"]["skipped"][-1]["reason"]


# -- RG7: the open-ended check kinds -------------------------------------------------------------------

TABLE = ("Report.\n\n## Findings and confidence\n\n| Finding | Confidence | Supported by | Contradicted by |\n"
         "| --- | --- | --- | --- |\n| Caverns cycle fast. | high (2 independent) | S1 a.example, S2 b.example | — |\n"
         "| Texas holds hydrogen. | medium (1 independent) | S3 c.example | — |\n")


def test_open_ended_checks_score_a_record():
    from evals.phase_tasks import evaluate
    rec = TaskRecord.create("x", tor=False)
    rec.report = TABLE + "\nSome critics disagree about cost. Teesside stores hydrogen."
    rec.status = "partial"
    rec.state["searches"] = [{"purpose": "main"}, {"purpose": "counter"}]
    task = {"allow_partial": True, "checks": {
        "report_has_facts": [["teesside"], ["microb", "bacteri"]], "findings_have_confidence": True,
        "not_all_high": True, "counter_search_done": True, "report_mentions_conflict": True,
        "min_independent_sources": 1}}
    out = evaluate(task, rec, [], 0, 0, [])
    assert out["fact 'teesside'"] and not out["fact 'microb'"]
    assert out["findings_have_confidence"] and out["not_all_high"] and out["counter_search_done"]
    assert out["report_mentions_conflict"] and out["finished"]               # partial is allowed here
    assert not out["min_independent_sources 1"]                              # no sources were kept
    rec.report = TABLE.replace("medium (1 independent)", "high (2 independent)")
    out = evaluate({"checks": {"not_all_high": True}}, rec, [], 0, 0, [])
    assert not out["not_all_high"] and not out["finished"]                   # partial without allow_partial


def test_the_campaign_check():
    from evals.phase_tasks import evaluate
    rec, _ = session(new_task(max_sessions=2), FETCH_ALL[:1] + [[call("session_notes", notes=NOTES)]], steps=1)
    rec, _ = session(TaskRecord.load(rec.id), FETCH_ALL[1:2] + [[call("session_notes", notes=NOTES)],
                                                                [call("finish", report="Full.")]],
                     steps=1, extractor=[RuntimeError("down")])
    assert rec.state["campaign"]["ended"]
    assert evaluate({"allow_partial": True, "checks": {"campaign": True}}, rec, [], 0, 0, [])["campaign"]
    one, _ = session(new_task(), [[call("finish", report="Done at once.")]], extractor=[])
    assert not evaluate({"checks": {"campaign": True}}, one, [], 0, 0, [])["campaign"]   # one session only


# -- RG10: the schedule command (the adapter itself: tests/test_schedule.py) ---------------------------

def test_cli_schedule_sets_the_campaign_and_installs(monkeypatch, tmp_path):
    import scout.cli
    import scout.schedule as sch
    from test_schedule import FakeRun

    fake = FakeRun()
    real = sch.Scheduler
    monkeypatch.setattr(sch, "Scheduler", lambda **kw: real(system="Darwin", home=tmp_path / "home", run=fake,
                                                            uid=501, path_env="/usr/bin", **kw))
    rec = new_task()
    assert scout.cli.main(["schedule", rec.id, "--daily", "09:30", "--session", "2h", "--max-sessions", "5"]) == 0
    c = TaskRecord.load(rec.id).state["campaign"]
    assert c["session_minutes"] == 120 and c["max_sessions"] == 5
    plist = tmp_path / "home" / "Library" / "LaunchAgents" / f"local.online-scout.{rec.id}.plist"
    import plistlib
    data = plistlib.loads(plist.read_bytes())
    assert data["ProgramArguments"][-5:] == ["resume", rec.id, "--session", "2h", "--unattended"]
    assert data["StandardOutPath"] == str(rec.dir / "schedule.log")
    assert scout.cli.main(["schedule", "--list"]) == 0
    assert scout.cli.main(["schedule", rec.id, "--remove"]) == 0 and not plist.exists()
    assert scout.cli.main(["schedule", rec.id, "--daily", "25:00", "--session", "2h"]) == 2
    assert scout.cli.main(["schedule", "no-such-task", "--daily", "09:00", "--session", "2h"]) == 2
