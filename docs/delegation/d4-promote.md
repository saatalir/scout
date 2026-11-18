---
files: ["scout/promote.py", "tests/test_promote.py"]
context: ["scout/playbooks.py", "playbooks/httpbin-pizza.yaml", "scout/browser/page.py", "scout/task.py"]
tests: ["tests/test_promote.py"]
---
# D4: playbook promotion — repeated successful traces become proposed playbooks

When a task finishes `done`, Scout writes its browser actions to `trace.yaml` in the task folder
(`scout/playbooks.py`, `trace_from_actions`): a playbook-shaped dict whose values are literal. Write a NEW
module `scout/promote.py` that finds traces of the **same flow** run at least twice and turns each such flow
into a **proposed** playbook with params, for a human to review. Proposals are written to
`<playbooks dir>/proposed/`, which `load_playbooks` does not read (it reads `*.yaml` in the playbooks dir only),
so nothing is active until a human moves the file up one folder. Do not change any other file.

## API (`scout/promote.py`, `from __future__ import annotations`, stdlib + `yaml` only)

```python
def load_traces(tasks_dir: Path | None = None) -> list[tuple[Path, dict]]:
    """Every `<task folder>/trace.yaml` directly under tasks_dir (default scout.task.tasks_dir()), parsed with
    yaml.safe_load, sorted by path. Files that fail to parse, or are not a dict with a non-empty `steps` list,
    are skipped silently."""

def flow_key(trace: dict) -> tuple:
    """What makes two traces the same flow:
    (first match url or "", tuple of the step *shapes*), where a step's shape is
      open  -> ("open", url without query string)
      type  -> ("type", field label)                 select -> ("select", field label)
      click -> ("click", element label)              submit -> ("submit", element label)
      fuzzy -> ("fuzzy",)                            expect/wait -> (kind,)
      check -> ("check",)      and a run of consecutive check steps counts as ONE ("check",) shape,
                               because the boxes ticked are the values of the flow, not its structure.
    Labels are compared stripped and lower-cased."""

def propose(traces: list[tuple[Path, dict]], min_runs: int = 2) -> list[dict]:
    """Group traces by flow_key; every group with >= min_runs traces becomes one proposed playbook dict
    (the same format as playbooks/*.yaml), in order of first appearance. See "Generalising" below."""

def write_proposals(proposals: list[dict], out_dir: Path | None = None,
                    active_dir: Path | None = None) -> list[Path]:
    """Write each proposal to out_dir (default playbooks_dir() / "proposed") as <name>.yaml, with a first-line
    comment `# Proposed from N successful runs; review, then move it to playbooks/ to activate.` followed by
    yaml.safe_dump(proposal, sort_keys=False, allow_unicode=True). Skip (do not write) a proposal when an active
    playbook in active_dir (default playbooks_dir(); use scout.playbooks.load_playbooks) already matches its
    first match url (`Playbook.matches(url)`). Overwrite an existing file of the same name in out_dir.
    Return the paths written."""

def promote(tasks_dir: Path | None = None, min_runs: int = 2, out_dir: Path | None = None,
            active_dir: Path | None = None) -> list[Path]:
    """load_traces -> propose -> write_proposals."""
```

## Generalising a group into a playbook

Steps are aligned by position after collapsing each trace's check runs (all traces in a group have the same
shapes, so position i is the same kind of step in every trace).

- `name`: `"proposed-" + slug` where slug is the group's first match url, lower-cased, every run of
  characters other than a-z and 0-9 replaced by `-`, stripped of `-`, at most 60 chars. If two proposals would get
  the same name, add `-2`, `-3`….
- `description`: `"Proposed from N successful runs: " + the first trace's description` (cut to 300 chars).
- `match`: `{"urls": [first match url]}`.
- `open`: kept literal (the url of the first trace).
- `type` / `select`: if the `text` / `option` is identical in all traces, keep it literal. Otherwise it becomes
  a param: the name is the field label slugged with `_` (lower-case, runs of non-alphanumerics → `_`, stripped,
  at most 40 chars; if empty use `field_<i>`; on a clash add `_2`…), the step's value becomes `"{<param>}"`, and
  `params[<param>] = {"description": <field label>, "required": True}`. For select, also add
  `"enum": sorted(set(values seen))`.
- a check run: if every trace ticked the same labels in the same order, keep the check steps literal. Otherwise
  one param `choices` (`choices_2`… if taken) with `{"description": "boxes or options to tick (seen: " + ", ".join
  of the distinct labels seen, in first-seen order + ")", "type": "list", "required": True}`, and the run
  becomes ONE step `{"check": {"field": "{item}"}, "for_each": "<param>"}`.
- `click`, `expect`, `wait`, `fuzzy`: kept literal from the first trace.
- `submit`: element literal. Summary: take the first trace's summary and replace each literal value that became
  a param in that same trace (its text, longest first, only values of 3+ chars) by `{<param>}`.
- `params` key order: in order of first use.
- The result must pass `scout.playbooks.parse_playbook` (exactly one step kind per step, `for_each` names a param).

## Tests (`tests/test_promote.py`, no network, no browser)

Write traces as `tmp_path/tasks/<task>/trace.yaml`. Build them like `trace_from_actions` does; the pizza flow:

```python
def pizza(name, phone, boxes, summary_name=None):
    return {"name": "draft-httpbin-org-forms-post", "description": "Trace of a successful run: order a pizza",
            "match": {"urls": ["httpbin.org/forms/post"]}, "params": {},
            "steps": [{"open": "https://httpbin.org/forms/post"},
                      {"type": {"field": "Customer name:", "text": name}},
                      {"type": {"field": "Telephone:", "text": phone}},
                      *[{"check": {"field": b}} for b in boxes],
                      {"submit": {"element": "Submit order",
                                  "summary": f"Submit the pizza order for {summary_name or name} on httpbin.org"}}]}
```

1. Two pizza traces with different names, the same phone, boxes `["Medium", "Mushroom"]` and
   `["Large", "Onion", "Bacon"]` → one proposal named `proposed-httpbin-org-forms-post`; param `customer_name`
   (required, description "Customer name:"); the phone stays literal; one for_each check step over `choices`
   whose description lists Medium, Mushroom, Large, Onion, Bacon; the submit summary is
   `"Submit the pizza order for {customer_name} on httpbin.org"`; `parse_playbook` accepts it and it matches
   `"order on https://httpbin.org/forms/post"`.
2. **It replays**: run that proposal with `scout.playbooks.run_playbook(pb, {"customer_name": "Test User",
   "choices": ["Medium", "Mushroom"]}, call=fake_call, view=fake_view, sleep=lambda s: None)` where `fake_view`
   returns a `scout.browser.page.PageView` with Elements: e1 input text "Customer name:", e2 input tel
   "Telephone:", e3 radio "Medium", e4 checkbox "Mushroom", e5 button "Submit order" (form "f1"); and `fake_call`
   records `(tool, args)` and returns `{"ok": True}`. Assert the recorded calls are, in order: browser_open,
   browser_type e1 "Test User", browser_type e2 <the literal phone>, browser_click e3, browser_click e4,
   click_submit e5 with action_summary "Submit the pizza order for Test User on httpbin.org". (Read
   `run_playbook` in scout/playbooks.py for the exact tool names and args it sends; adapt the assertion to it.)
3. Only one trace of a flow → no proposal; `min_runs=1` → one proposal.
4. Two different flows (another url / different field labels) → two proposals, not merged.
5. Identical check runs in both traces → the check steps stay literal, no `choices` param.
6. A select step with different options → param with `enum` of the options seen.
7. `write_proposals` writes to `out_dir` with the comment line first; the file loads back with yaml and parses;
   a proposal whose url an active playbook in `active_dir` already matches is skipped; `load_playbooks(active_dir)`
   never returns proposals (they are in the subfolder).
8. `load_traces` skips a broken YAML file and a trace without steps.
