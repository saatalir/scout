"""Phase 3 D4 — playbook promotion: traces of the same flow, run successfully at least twice, become a proposed
playbook with params, written to <playbooks>/proposed/ (not loaded, git-ignored) for a human to review and move up.

Privacy: every typed value becomes a param, even one that was the same in every run (it may be the user's own
phone number or address); such a param is optional and its step is skipped when no value is given. No typed value
from a trace is written into a proposal.

Delegated to Strata (docs/delegation/d4-promote.md); reviewed, typed values made params.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from .playbooks import STEP_KINDS, load_playbooks, playbooks_dir as _playbooks_dir
from .task import tasks_dir as _tasks_dir

_COMMENT_RE = re.compile(r"^Proposed from (\d+) successful runs:")


def _first_url(trace: dict) -> str:
    match = trace.get("match") or {}
    urls = match.get("urls") or []
    if isinstance(urls, str):
        return urls
    if isinstance(urls, list) and urls:
        return str(urls[0])
    return ""


def _step_kind(step: Any) -> str | None:
    if not isinstance(step, dict):
        return None
    for kind in STEP_KINDS:
        if kind in step:
            return kind
    return None


def _url_without_query(url: Any) -> str:
    return str(url or "").split("?", 1)[0]


def _norm_label(label: Any) -> str:
    return str(label or "").strip().lower()


def _step_value(step: dict, kind: str) -> Any:
    return step.get(kind)


def _field_label(step: dict, kind: str) -> str:
    value = _step_value(step, kind)
    if isinstance(value, dict):
        return str(value.get("field") or value.get("element") or "")
    return str(value or "")


def _element_label(step: dict, kind: str) -> str:
    value = _step_value(step, kind)
    if isinstance(value, dict):
        return str(value.get("element") or value.get("field") or "")
    return str(value or "")


def _type_text(step: dict) -> str:
    value = _step_value(step, "type")
    if isinstance(value, dict):
        return str(value.get("text") or "")
    return ""


def _select_option(step: dict) -> str:
    value = _step_value(step, "select")
    if isinstance(value, dict):
        return str(value.get("option") or "")
    return ""


def _check_label(step: dict) -> str:
    value = _step_value(step, "check")
    if isinstance(value, dict):
        return str(value.get("field") or "")
    return str(value or "")


def _step_shape(kind: str, step: dict) -> tuple:
    if kind == "open":
        value = _step_value(step, kind)
        url = value.get("url") if isinstance(value, dict) else value
        return ("open", _url_without_query(url))
    if kind in ("type", "select"):
        return (kind, _norm_label(_field_label(step, kind)))
    if kind in ("click", "submit"):
        return (kind, _norm_label(_element_label(step, kind)))
    if kind == "fuzzy":
        return ("fuzzy",)
    if kind in ("expect", "wait"):
        return (kind,)
    if kind == "check":
        return ("check",)
    return (kind,)


def flow_key(trace: dict) -> tuple:
    shapes: list[tuple] = []
    in_check_run = False
    for step in trace.get("steps") or []:
        kind = _step_kind(step)
        if kind == "check":
            if not in_check_run:
                shapes.append(("check",))
                in_check_run = True
            continue
        in_check_run = False
        if kind is None:
            shapes.append(("unknown",))
        else:
            shapes.append(_step_shape(kind, step))
    return (_first_url(trace), tuple(shapes))


def load_traces(tasks_dir: Path | None = None) -> list[tuple[Path, dict]]:
    base = tasks_dir if tasks_dir is not None else _tasks_dir()
    if not base.is_dir():
        return []

    out: list[tuple[Path, dict]] = []
    for path in sorted(base.glob("*/trace.yaml")):
        try:
            data = yaml.safe_load(path.read_text())
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        steps = data.get("steps")
        if not isinstance(steps, list) or not steps:
            continue
        out.append((path, data))
    return out


def _aligned_steps(trace: dict) -> list[dict]:
    entries: list[dict] = []
    current_check: list[dict] | None = None

    for step in trace.get("steps") or []:
        kind = _step_kind(step)
        if kind == "check":
            if current_check is None:
                current_check = []
                entries.append({"kind": "check", "steps": current_check})
            current_check.append(step)
            continue

        current_check = None
        entries.append({"kind": kind, "step": step})

    return entries


def _slug_name(url: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", url.lower()).strip("-")[:60]


def _unique_name(base: str, used: set[str]) -> str:
    name = base
    n = 2
    while name in used:
        name = f"{base}-{n}"
        n += 1
    used.add(name)
    return name


def _slug_param(label: str, index: int, used: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", str(label or "").lower()).strip("_")[:40]
    if not base:
        base = f"field_{index}"

    name = base
    n = 2
    while name in used:
        name = f"{base}_{n}"
        n += 1
    used.add(name)
    return name


def _unique_param_name(base: str, used: set[str]) -> str:
    name = base
    n = 2
    while name in used:
        name = f"{base}_{n}"
        n += 1
    used.add(name)
    return name


def _generalise(group: list[tuple[Path, dict]], name: str) -> dict:
    first_trace = group[0][1]
    first_url = _first_url(first_trace)

    aligned = [_aligned_steps(trace) for _, trace in group]
    params: dict[str, dict] = {}
    used_params: set[str] = set()
    param_values: dict[str, str] = {}
    steps_out: list[dict] = []
    submit_indices: list[int] = []

    for pos, entries in enumerate(zip(*aligned), 1):
        kind = entries[0]["kind"]
        first_step = entries[0].get("step")

        if kind == "open":
            steps_out.append(first_step)

        elif kind == "type":
            field_label = _field_label(first_step, "type")
            texts = [_type_text(e["step"]) for e in entries]
            pname = _slug_param(field_label, pos, used_params)
            if len(set(texts)) == 1:          # the same every time: optional, and the value is not kept
                steps_out.append({"type": {"field": field_label, "text": f"{{{pname}}}"}, "optional": True})
                params[pname] = {"description": f"{field_label} (the same in every run)"}
            else:
                steps_out.append({"type": {"field": field_label, "text": f"{{{pname}}}"}})
                params[pname] = {"description": field_label, "required": True}
            param_values[pname] = texts[0]

        elif kind == "select":
            field_label = _field_label(first_step, "select")
            options = [_select_option(e["step"]) for e in entries]
            if len(set(options)) == 1:
                steps_out.append(first_step)
            else:
                pname = _slug_param(field_label, pos, used_params)
                steps_out.append({"select": {"field": field_label, "option": f"{{{pname}}}"}})
                params[pname] = {
                    "description": field_label,
                    "required": True,
                    "enum": sorted(set(options)),
                }
                param_values[pname] = options[0]

        elif kind == "check":
            runs: list[list[str]] = []
            for entry in entries:
                runs.append([_check_label(step) for step in entry["steps"]])

            if all(labels == runs[0] for labels in runs):
                steps_out.extend(entries[0]["steps"])
            else:
                pname = _unique_param_name("choices", used_params)
                seen: list[str] = []
                seen_set: set[str] = set()
                for labels in runs:
                    for label in labels:
                        if label not in seen_set:
                            seen_set.add(label)
                            seen.append(label)
                params[pname] = {
                    "description": "boxes or options to tick (seen: " + ", ".join(seen) + ")",
                    "type": "list",
                    "required": True,
                }
                steps_out.append({"check": {"field": "{item}"}, "for_each": pname})

        elif kind == "submit":
            value = _step_value(first_step, "submit")
            if isinstance(value, dict):
                element = str(value.get("element") or "")
                summary = str(value.get("summary") or "")
            else:
                element = str(value or "")
                summary = ""
            submit_indices.append(len(steps_out))
            steps_out.append({"submit": {"element": element, "summary": summary}})

        else:
            steps_out.append(first_step)

    replacements = [(value, pname) for pname, value in param_values.items() if len(value) >= 3]
    replacements.sort(key=lambda item: len(item[0]), reverse=True)
    for idx in submit_indices:
        step = steps_out[idx]
        summary = str(step["submit"].get("summary") or "")
        for value, pname in replacements:
            summary = summary.replace(value, f"{{{pname}}}")
        step["submit"]["summary"] = summary

    # Not the traces' own descriptions: they quote the task's objective, which may carry personal data.
    description = (f"Proposed from {len(group)} successful runs: the flow on {first_url}. Review the steps, which "
                   "params are required, and the values kept literal.")
    return {
        "name": name,
        "description": description[:300],
        "match": {"urls": [first_url]},
        "params": params,
        "steps": steps_out,
    }


def propose(traces: list[tuple[Path, dict]], min_runs: int = 2) -> list[dict]:
    groups: dict[tuple, list[tuple[Path, dict]]] = {}
    for item in traces:
        key = flow_key(item[1])
        groups.setdefault(key, []).append(item)

    used_names: set[str] = set()
    proposals: list[dict] = []
    for group in groups.values():
        if len(group) < min_runs:
            continue
        first_url = _first_url(group[0][1])
        base_name = "proposed-" + _slug_name(first_url)
        name = _unique_name(base_name, used_names)
        proposals.append(_generalise(group, name))

    return proposals


def _proposal_runs(proposal: dict) -> str:
    match = _COMMENT_RE.match(str(proposal.get("description") or ""))
    return match.group(1) if match else "N"


def write_proposals(proposals: list[dict], out_dir: Path | None = None,
                    active_dir: Path | None = None) -> list[Path]:
    out = out_dir if out_dir is not None else _playbooks_dir() / "proposed"
    active = active_dir if active_dir is not None else _playbooks_dir()
    active_playbooks = load_playbooks(active)

    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    for proposal in proposals:
        url = _first_url(proposal)
        if url and any(pb.matches(url) for pb in active_playbooks):
            continue

        name = str(proposal.get("name") or "proposed")
        path = out / f"{name}.yaml"
        comment = (
            f"# Proposed from {_proposal_runs(proposal)} successful runs; "
            "review, then move it to playbooks/ to activate.\n"
        )
        path.write_text(comment + yaml.safe_dump(proposal, sort_keys=False, allow_unicode=True))
        written.append(path)

    return written


def promote(tasks_dir: Path | None = None, min_runs: int = 2, out_dir: Path | None = None,
            active_dir: Path | None = None) -> list[Path]:
    traces = load_traces(tasks_dir)
    proposals = propose(traces, min_runs=min_runs)
    return write_proposals(proposals, out_dir=out_dir, active_dir=active_dir)
