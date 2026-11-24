"""X2 (event trace per task) and X4 (Strata fingerprint and pin)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from scout import strata_info, trace
from scout.task import TaskRecord
from test_phase1 import call
from test_rigor import FETCH_ALL, run


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOUT_LIBRARY_DIR", str(tmp_path / "library"))
    monkeypatch.setenv("SCOUT_TASKS_DIR", str(tmp_path / "tasks"))


# -- X2 ---------------------------------------------------------------------------------------------

def test_a_run_leaves_an_event_trace(capsys):
    rec, _ = run(FETCH_ALL[:2] + [[call("finish", report="Caverns cycle quickly.")]], [], rigor="quick")
    events = trace.read(rec.dir)
    kinds = [e["kind"] for e in events]
    assert kinds.count("model") == 3 and kinds.count("tool") == 2 and kinds[-1] == "end"
    tool = next(e for e in events if e["kind"] == "tool")
    assert tool["name"] == "fetch_page" and tool["ok"] and tool["n"] == 1
    model = next(e for e in events if e["kind"] == "model")
    assert model["tools"] == ["fetch_page"] and "seconds" in model and model["offered"] > 10
    assert any(e["kind"] == "note" and e["text"].startswith("[1] fetch_page ok") for e in events)
    assert events[-1]["status"] == "done"
    s = trace.summary(events)
    assert s["model_turns"] == 3 and s["tool_calls"] == 2 and s["per_tool"]["fetch_page"]["calls"] == 2

    import scout.cli
    assert scout.cli.main(["show", rec.id]) == 0
    assert "Time: model" in capsys.readouterr().out


def test_a_cut_trace_line_is_skipped(tmp_path):
    trace.event(tmp_path, "model", seconds=1.0)
    with open(tmp_path / trace.FILE, "a") as f:
        f.write('{"t": 1, "kind": "to')            # a crash mid-write
    assert [e["kind"] for e in trace.read(tmp_path)] == ["model"]
    assert trace.read(tmp_path / "missing") == []


def test_a_model_error_is_traced():
    from scout.agent import Scout
    from scout.resilience import RetryPolicy
    from test_campaign import Recording, client
    rec = TaskRecord.create("x", tor=False)
    try:
        Scout(rec, llm=Recording([ValueError("boom")]), client_factory=client,
              model_policy=RetryPolicy(attempts=1, base=0, sleep=lambda s: None)).run()
    except ValueError:
        pass                                       # a permanent model error ends the run; the trace keeps it
    assert any(e["kind"] == "model_error" and "boom" in e["error"] for e in trace.read(rec.dir))


# -- X4 ---------------------------------------------------------------------------------------------

PROPS = {"default_generation_settings": {"n_ctx": 262144, "params": {"temperature": 1.0, "top_p": 0.95}},
         "total_slots": 2, "model_alias": "m-q4", "chat_template": "{{ messages }}"}
HEALTH = {"status": "ok", "model": "m-q4", "max_context": 262144, "service": "strata"}


def fake_get(props=PROPS, health=HEALTH):
    seen = []

    def get(url, **kw):
        seen.append((url, kw.get("headers")))
        return SimpleNamespace(json=lambda: props if url.endswith("/props") else health)
    get.seen = seen
    return get


def test_fingerprint_names_what_matters_and_no_secret(monkeypatch):
    monkeypatch.delenv("STRATA_VERSION", raising=False)
    get = fake_get()
    fp = strata_info.fingerprint("https://strata.example/v1", "sk-secret", get=get)
    assert fp["model"] == "m-q4" and fp["n_ctx"] == 262144 and fp["slots"] == 2 and fp["service"] == "strata"
    assert fp["template_sha256"] and "{{" not in json.dumps(fp)          # the template only as a hash
    assert "sk-secret" not in json.dumps(fp) and "strata.example" not in json.dumps(fp)
    assert [u for u, _ in get.seen] == ["https://strata.example/props", "https://strata.example/health"]
    assert get.seen[0][1] == {"Authorization": "Bearer sk-secret"}
    monkeypatch.setenv("STRATA_VERSION", "strata 0.2.12")
    assert strata_info.fingerprint("https://strata.example/v1", None, get=fake_get())["version"] == "strata 0.2.12"


def test_pin_round_trip_and_differences(tmp_path, monkeypatch):
    monkeypatch.delenv("STRATA_VERSION", raising=False)
    path = tmp_path / "pin.json"
    fp = strata_info.fingerprint("http://s/v1", None, get=fake_get())
    assert strata_info.differences(fp, None)[0].startswith("no pin yet")
    strata_info.save_pin(fp, "phase 4 tasks, 2026-11-25", path)
    pin = strata_info.load_pin(path)
    assert pin["id"] == strata_info.fingerprint_id(fp) and strata_info.differences(fp, pin) == []
    changed = strata_info.fingerprint("http://s/v1", None, get=fake_get(
        props={**PROPS, "total_slots": 4, "chat_template": "new"}))
    diff = strata_info.differences(changed, pin)
    assert "slots: 2 -> 4" in diff and "template_sha256" in diff
    monkeypatch.setenv("STRATA_VERSION", "strata 0.3.0")            # a version the pin never recorded: no alarm
    assert strata_info.differences(strata_info.fingerprint("http://s/v1", None, get=fake_get()), pin) == []
