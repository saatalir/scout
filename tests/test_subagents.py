"""Parallel research sub-agents: fan-out, shared library task id, concurrency limit, retry of a failed part."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest

from scout.agent import Scout
from scout.library import items_for_task
from scout.resilience import RetryPolicy
from scout.task import TaskRecord
from test_phase1 import call, handler


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOUT_LIBRARY_DIR", str(tmp_path / "library"))
    monkeypatch.setenv("SCOUT_TASKS_DIR", str(tmp_path / "tasks"))


class Router:
    """Fake model serving several conversations at once: each script is chosen by a key found in the
    conversation's objective (messages[1]). Tracks how many calls run concurrently."""

    def __init__(self, scripts: dict[str, list]):
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.lock = threading.Lock()
        self.active = self.peak = 0
        self.tools_seen: dict[str, list[str]] = {}

    def chat(self, role, messages, **kw):
        objective = messages[1]["content"]
        # children quote the parent's objective, so match on how the objective *starts*; a retry note wins
        key = next((k for k in self.scripts if k.startswith("A first") and k in objective), None) \
            or next(k for k in self.scripts if objective.startswith(k))
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.tools_seen[key] = [t["function"]["name"] for t in kw.get("tools") or []]
            turn = self.scripts[key].pop(0) if self.scripts[key] else "…"
        time.sleep(0.05)                              # overlap so concurrency is observable
        with self.lock:
            self.active -= 1
        msg = (SimpleNamespace(content="", tool_calls=turn) if isinstance(turn, list)
               else SimpleNamespace(content=turn, tool_calls=None))
        return SimpleNamespace(message=msg, text=msg.content or "", tool_calls=list(msg.tool_calls or []),
                               raw=SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop")]))


@contextmanager
def client(tor=False):
    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as c:
        yield c


def scout(router, objective="Survey hydrogen storage: salt caverns, LOHC, ammonia."):
    rec = TaskRecord.create(objective, tor=False)
    return Scout(rec, llm=router, client_factory=client,
                 model_policy=RetryPolicy(attempts=2, base=0, sleep=lambda s: None)).run()


def save(title, doi):
    return [call("save_to_library", title=title, kind="paper", doi=doi, source="crossref")]


def test_parts_run_in_parallel_and_items_belong_to_the_parent():
    router = Router({
        "Survey hydrogen": [[call("research_subtasks", subtasks=["salt caverns", "LOHC", "ammonia"])],
                            [call("finish", report="[done] 3 parts")]],
        "salt caverns\n": [save("Salt cavern storage", "10.1/salt"), [call("finish", report="saved 10.1/salt")]],
        "LOHC\n": [save("LOHC review", "10.1/lohc"), [call("finish", report="saved 10.1/lohc")]],
        "ammonia\n": [save("Ammonia carriers", "10.1/nh3"), [call("finish", report="saved 10.1/nh3")]],
    })
    rec = scout(router)
    assert rec.status == "done"
    step = rec.steps[0]
    assert step.tool == "research_subtasks" and step.ok and step.summary.count('"saved": 1') == 3
    assert {e["doi"] for e in items_for_task(rec.id)} == {"10.1/salt", "10.1/lohc", "10.1/nh3"}
    assert len(rec.files) == 3                                 # the parent's record lists the children's items
    assert router.peak == 2                                    # never more than SUB_PARALLEL model calls at once


def test_sub_agents_cannot_delegate_or_hand_off():
    router = Router({
        "Survey hydrogen": [[call("research_subtasks", subtasks=["salt caverns", "LOHC"])],
                            [call("finish", report="[done]")]],
        "salt caverns\n": [[call("finish", report="nothing found")]],
        "LOHC\n": [[call("finish", report="nothing found")]],
    })
    scout(router)
    for key in ("salt caverns\n", "LOHC\n"):
        assert not {"research_subtasks", "request_approval", "hand_to_human"} & set(router.tools_seen[key])
    assert "research_subtasks" in router.tools_seen["Survey hydrogen"]


def test_a_part_that_gets_nowhere_is_retried_once():
    router = Router({
        "Survey hydrogen": [[call("research_subtasks", subtasks=["salt caverns", "LOHC"])],
                            [call("finish", report="[done]")]],
        "salt caverns\n": [save("Salt cavern storage", "10.1/salt"), [call("finish", report="ok")]],
        # first attempt: three text replies -> failed with nothing saved; the retry's objective carries a note
        "LOHC\n": ["thinking", "still thinking", "giving up"],
        "A first attempt failed": [save("LOHC review", "10.1/lohc"), [call("finish", report="ok on retry")]],
    })
    rec = scout(router)
    assert '"saved_total": 2' in rec.steps[0].summary
    assert {e["doi"] for e in items_for_task(rec.id)} == {"10.1/salt", "10.1/lohc"}


def test_research_subtasks_needs_two_questions():
    router = Router({"Survey hydrogen": [[call("research_subtasks", subtasks=["only one"])],
                                         [call("finish", report="[not done]")],
                                         [call("finish", report="[not done] could not split")]]})
    rec = scout(router)
    assert rec.steps[0].ok is False and "at least two" in rec.steps[0].summary


def test_concurrent_saves_of_the_same_item_do_not_duplicate():
    from concurrent.futures import ThreadPoolExecutor
    from scout.library import Item, save_item
    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(lambda i: save_item(Item(title="Same", doi="10.1/same"), task_id="t")[1], range(8)))
    assert results.count(True) == 1


# -- sub-agents stay inside the parent's time (a campaign session overran 3x before this) ----------------------

def _parent(minutes, used_seconds):
    from scout.resilience import Deadline
    now = [0.0]
    rec = TaskRecord.create("Survey hydrogen storage.", tor=False)
    s = Scout(rec, llm=Router({}), client_factory=client, task_minutes=minutes)
    s.deadline = Deadline(minutes * 60, clock=lambda: now[0])
    now[0] = used_seconds
    return s


def test_sub_agent_time_is_capped_by_the_parents_deadline():
    from scout import agent
    assert _parent(90, 0)._sub_minutes() == agent.SUB_MINUTES             # plenty of time: the normal budget
    assert _parent(10, 0)._sub_minutes() == pytest.approx(7.0)            # 10-min session less a 3-min reserve
    assert _parent(10, 400)._sub_minutes() is None                        # under 2 min would be left: not started
    assert _parent(90, 90 * 60 - 25 * 60)._sub_minutes() == pytest.approx(25 - 13.5)   # reserve = 15 % of 90 min


def test_no_sub_agents_when_the_time_is_nearly_used_up():
    router = Router({
        "Survey hydrogen": [[call("research_subtasks", subtasks=["salt caverns", "LOHC"])],
                            [call("finish", report="[done]")]],
        "salt caverns\n": [[call("finish", report="should not run")]],
    })
    rec = TaskRecord.create("Survey hydrogen storage.", tor=False)
    rec = Scout(rec, llm=router, client_factory=client, task_minutes=4,       # 4 min < reserve + minimum
                model_policy=RetryPolicy(attempts=2, base=0, sleep=lambda s: None)).run()
    assert rec.steps[0].ok is False and "too little time" in rec.steps[0].summary
    assert "salt caverns\n" not in router.tools_seen
