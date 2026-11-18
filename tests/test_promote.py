from __future__ import annotations

from pathlib import Path

import yaml

from scout.browser.page import Element, PageView
from scout.playbooks import load_playbooks, parse_playbook, run_playbook
from scout.promote import flow_key, load_traces, propose, write_proposals


def pizza(name, phone, boxes, summary_name=None):
    return {
        "name": "draft-httpbin-org-forms-post",
        "description": "Trace of a successful run: order a pizza",
        "match": {"urls": ["httpbin.org/forms/post"]},
        "params": {},
        "steps": [
            {"open": "https://httpbin.org/forms/post"},
            {"type": {"field": "Customer name:", "text": name}},
            {"type": {"field": "Telephone:", "text": phone}},
            *[{"check": {"field": b}} for b in boxes],
            {
                "submit": {
                    "element": "Submit order",
                    "summary": f"Submit the pizza order for {summary_name or name} on httpbin.org",
                }
            },
        ],
    }


def write_trace(root: Path, task: str, trace: dict) -> Path:
    d = root / "tasks" / task
    d.mkdir(parents=True, exist_ok=True)
    path = d / "trace.yaml"
    path.write_text(yaml.safe_dump(trace, sort_keys=False, allow_unicode=True))
    return path


def load_trace(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def test_two_pizza_traces_propose_one_playbook(tmp_path):
    p1 = write_trace(tmp_path, "t1", pizza("Alice", "555", ["Medium", "Mushroom"]))
    p2 = write_trace(tmp_path, "t2", pizza("Bob", "555", ["Large", "Onion", "Bacon"]))

    proposals = propose([(p1, load_trace(p1)), (p2, load_trace(p2))])
    assert len(proposals) == 1
    pb = proposals[0]

    assert pb["name"] == "proposed-httpbin-org-forms-post"
    assert pb["params"]["customer_name"] == {"description": "Customer name:", "required": True}
    assert pb["params"]["choices"] == {
        "description": "boxes or options to tick (seen: Medium, Mushroom, Large, Onion, Bacon)",
        "type": "list",
        "required": True,
    }

    phone = next(step for step in pb["steps"] if "type" in step and step["type"]["field"] == "Telephone:")
    assert phone == {"type": {"field": "Telephone:", "text": "{telephone}"}, "optional": True}
    assert pb["params"]["telephone"] == {"description": "Telephone: (the same in every run)"}
    assert "555" not in yaml.safe_dump(pb)                      # no typed value is kept
    assert "order a pizza" not in pb["description"]            # nor the objective (it may carry personal data)

    check_steps = [step for step in pb["steps"] if "check" in step]
    assert len(check_steps) == 1
    assert check_steps[0]["check"] == {"field": "{item}"}
    assert check_steps[0]["for_each"] == "choices"

    submit = next(step["submit"] for step in pb["steps"] if "submit" in step)
    assert submit["summary"] == "Submit the pizza order for {customer_name} on httpbin.org"

    parsed = parse_playbook(pb)
    assert parsed.matches("order on https://httpbin.org/forms/post")


def test_proposal_replays(tmp_path):
    p1 = write_trace(tmp_path, "t1", pizza("Alice", "555", ["Medium", "Mushroom"]))
    p2 = write_trace(tmp_path, "t2", pizza("Bob", "555", ["Large", "Onion", "Bacon"]))

    proposals = propose([(p1, load_trace(p1)), (p2, load_trace(p2))])
    pb = parse_playbook(proposals[0])

    calls = []

    def fake_call(tool, args):
        calls.append((tool, args))
        return {"ok": True}

    elements = [
        Element(id="e1", tag="input", name="Customer name:", type="text"),
        Element(id="e2", tag="input", name="Telephone:", type="tel"),
        Element(id="e3", tag="input", name="Medium", type="radio", checked=False),
        Element(id="e4", tag="input", name="Mushroom", type="checkbox", checked=False),
        Element(id="e5", tag="button", name="Submit order", form="f1"),
    ]
    view = PageView(
        url="https://httpbin.org/forms/post",
        title="Post",
        elements=elements,
        forms=[{"id": "f1"}],
    )

    result = run_playbook(
        pb,
        {"customer_name": "Test User", "choices": ["Medium", "Mushroom"]},
        call=fake_call,
        view=lambda: view,
        sleep=lambda seconds: None,
    )

    assert result.get("done") is True
    assert calls == [
        ("browser_open", {"url": "https://httpbin.org/forms/post"}),
        ("browser_type", {"element_id": "e1", "text": "Test User"}),
        ("browser_click", {"element_id": "e3"}),
        ("browser_click", {"element_id": "e4"}),
        (
            "click_submit",
            {
                "element_id": "e5",
                "action_summary": "Submit the pizza order for Test User on httpbin.org",
            },
        ),
    ]


def test_min_runs(tmp_path):
    p1 = write_trace(tmp_path, "t1", pizza("Alice", "555", ["Medium"]))
    trace = load_trace(p1)

    assert propose([(p1, trace)]) == []
    assert len(propose([(p1, trace)], min_runs=1)) == 1


def test_two_different_flows_are_not_merged(tmp_path):
    p1 = write_trace(tmp_path, "pizza1", pizza("Alice", "555", ["Medium"]))
    p2 = write_trace(tmp_path, "pizza2", pizza("Bob", "555", ["Large"]))

    other1 = pizza("Carol", "555", ["Medium"])
    other1["match"]["urls"] = ["httpbin.org/other"]
    other1["steps"][0]["open"] = "https://httpbin.org/other"
    p3 = write_trace(tmp_path, "other1", other1)

    other2 = pizza("Dave", "555", ["Large"])
    other2["match"]["urls"] = ["httpbin.org/other"]
    other2["steps"][0]["open"] = "https://httpbin.org/other"
    p4 = write_trace(tmp_path, "other2", other2)

    proposals = propose([
        (p1, load_trace(p1)),
        (p2, load_trace(p2)),
        (p3, load_trace(p3)),
        (p4, load_trace(p4)),
    ])

    assert len(proposals) == 2
    assert {p["name"] for p in proposals} == {
        "proposed-httpbin-org-forms-post",
        "proposed-httpbin-org-other",
    }


def test_identical_check_runs_stay_literal(tmp_path):
    p1 = write_trace(tmp_path, "t1", pizza("Alice", "555", ["Medium", "Mushroom"]))
    p2 = write_trace(tmp_path, "t2", pizza("Bob", "555", ["Medium", "Mushroom"]))

    proposals = propose([(p1, load_trace(p1)), (p2, load_trace(p2))])
    pb = proposals[0]

    assert "choices" not in pb["params"]
    check_steps = [step for step in pb["steps"] if "check" in step]
    assert len(check_steps) == 2
    assert check_steps[0]["check"] == {"field": "Medium"}
    assert check_steps[1]["check"] == {"field": "Mushroom"}
    assert "for_each" not in check_steps[0]


def test_select_with_different_options_becomes_param_with_enum(tmp_path):
    def select_trace(option):
        return {
            "name": "draft-httpbin-org-select",
            "description": "Trace of a successful run: choose a size",
            "match": {"urls": ["httpbin.org/select"]},
            "params": {},
            "steps": [
                {"open": "https://httpbin.org/select"},
                {"select": {"field": "Size:", "option": option}},
                {"submit": {"element": "Go", "summary": f"Go with {option}"}},
            ],
        }

    p1 = write_trace(tmp_path, "s1", select_trace("Medium"))
    p2 = write_trace(tmp_path, "s2", select_trace("Large"))

    proposals = propose([(p1, load_trace(p1)), (p2, load_trace(p2))])
    assert len(proposals) == 1
    pb = proposals[0]

    assert pb["params"]["size"]["enum"] == ["Large", "Medium"]
    select_step = next(step["select"] for step in pb["steps"] if "select" in step)
    assert select_step == {"field": "Size:", "option": "{size}"}
    submit = next(step["submit"] for step in pb["steps"] if "submit" in step)
    assert submit["summary"] == "Go with {size}"
    parse_playbook(pb)


def test_write_proposals(tmp_path):
    p1 = write_trace(tmp_path, "t1", pizza("Alice", "555", ["Medium", "Mushroom"]))
    p2 = write_trace(tmp_path, "t2", pizza("Bob", "555", ["Large", "Onion", "Bacon"]))
    proposals = propose([(p1, load_trace(p1)), (p2, load_trace(p2))])

    active = tmp_path / "playbooks"
    active.mkdir()
    out = active / "proposed"

    paths = write_proposals(proposals, out_dir=out, active_dir=active)
    assert paths == [out / "proposed-httpbin-org-forms-post.yaml"]

    text = paths[0].read_text()
    assert text.startswith(
        "# Proposed from 2 successful runs; review, then move it to playbooks/ to activate.\n"
    )

    loaded = yaml.safe_load(text)
    parse_playbook(loaded)

    # Proposals live in a subfolder and are not loaded as active playbooks.
    assert load_playbooks(active) == []

    (active / "active.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "active",
                "match": {"urls": ["httpbin.org/forms/post"]},
                "steps": [{"open": "https://httpbin.org/forms/post"}],
            },
            sort_keys=False,
            allow_unicode=True,
        )
    )
    assert [pb.name for pb in load_playbooks(active)] == ["active"]

    out2 = tmp_path / "proposed2"
    assert write_proposals(proposals, out_dir=out2, active_dir=active) == []
    assert not (out2 / "proposed-httpbin-org-forms-post.yaml").exists()


def test_load_traces_skips_bad_files(tmp_path):
    tasks = tmp_path / "tasks"

    good = tasks / "good"
    good.mkdir(parents=True)
    good_path = good / "trace.yaml"
    good_path.write_text(yaml.safe_dump(pizza("Alice", "555", ["Medium"])))

    bad = tasks / "bad"
    bad.mkdir(parents=True)
    (bad / "trace.yaml").write_text("key: [unclosed")

    no_steps = tasks / "no_steps"
    no_steps.mkdir(parents=True)
    (no_steps / "trace.yaml").write_text(yaml.safe_dump({"name": "no-steps"}))

    traces = load_traces(tasks)
    assert len(traces) == 1
    assert traces[0][0] == good_path
    assert traces[0][1]["name"] == "draft-httpbin-org-forms-post"


def test_flow_key_collapses_check_runs_and_normalises_labels():
    a = pizza("Alice", "555", ["Medium", "Mushroom"])
    b = pizza("Bob", "555", ["Large", "Onion", "Bacon"])
    assert flow_key(a) == flow_key(b)

    c = pizza("Alice", "555", ["Medium"])
    c["steps"][1]["type"]["field"] = " Customer Name: "
    assert flow_key(a) == flow_key(c)

    d = pizza("Alice", "555", ["Medium"])
    d["steps"][0]["open"] = "https://httpbin.org/forms/post?x=1"
    assert flow_key(a) == flow_key(d)
