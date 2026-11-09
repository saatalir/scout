"""Run the fixed task eval set (evals/tasks/tasks.yaml) through the real agent and score it.

    python -m evals.phase_tasks                    # every phase-1 task
    python -m evals.phase_tasks --only r01-known-paper-pdf,r07-duplicate
    python -m evals.phase_tasks --skip-needs browser  # without the browser VM

Each run gets its own library inbox and task folder under results/<ts>-tasks/,
so runs never mix and order-dependent tasks (`after`) see the earlier task's
items. Approval requests are recorded and auto-approved; the checks decide
whether asking was right. Needs Strata and the network (and the browser VM for
tasks that need it).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
TASKS_FILE = Path(__file__).resolve().parent / "tasks" / "tasks.yaml"
THRESHOLDS = Path(__file__).resolve().parent / "thresholds.yaml"


def load_tasks(path: Path = TASKS_FILE) -> list[dict]:
    return yaml.safe_load(path.read_text())["tasks"]


@dataclass
class Outcome:
    task_id: str
    phase: int
    status: str = "not run"
    passed: bool = False
    checks: dict[str, bool] = field(default_factory=dict)
    steps: int = 0
    seconds: float = 0.0
    error: str | None = None
    report: str = ""
    record: str | None = None


# -- checks ------------------------------------------------------------------------

def _greek_share(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    return sum("Ͱ" <= c <= "Ͽ" or "ἀ" <= c <= "῿" for c in letters) / max(1, len(letters))


def evaluate(task: dict, rec, items: list[dict], files_before: int, files_after: int,
             approvals: list[tuple[int, str]]) -> dict[str, bool]:
    from scout.library import norm_arxiv, norm_doi, read_front_matter, library_dir

    lib = library_dir()
    metas = [read_front_matter(lib / e["path"]) | {"_entry": e} for e in items]
    report = (rec.report or "").lower()
    out: dict[str, bool] = {}
    for kind, spec in (task.get("checks") or {}).items():
        if kind == "library_has":
            if "arxiv" in spec:
                ok = any(m.get("arxiv") == norm_arxiv(spec["arxiv"]) for m in metas)
            elif "doi" in spec:
                ok = any(m.get("doi") == norm_doi(spec["doi"]) for m in metas)
            else:
                ok = any(spec["url_contains"].lower() in (m.get("source_url") or "").lower() for m in metas)
            out[f"library_has {spec}"] = ok
        elif kind == "min_items":
            good = [m for m in metas
                    if (not spec.get("year_from") or (m.get("year") or 0) >= spec["year_from"])
                    and (not spec.get("source") or m.get("source") == spec["source"])]
            out[f"min_items {spec}"] = len(good) >= spec["n"]
        elif kind == "verified_files":
            files = [m for m in metas if m.get("file")]
            if spec.get("kind") and spec["kind"] != "any":
                files = [m for m in files if m["file"].lower().endswith("." + spec["kind"])]
            out[f"verified_files {spec}"] = len(files) >= spec["n"]
        elif kind == "pdf_title_matches":
            # Read the stored PDFs themselves: the library copy is what the user gets.
            from scout.tools.web import pdf_text, title_in_text
            pdfs = [lib / Path(m["_entry"]["path"]).parent / m["file"] for m in metas
                    if (m.get("file") or "").lower().endswith(".pdf")]
            out[f"pdf_title_matches {spec!r}"] = any(title_in_text(spec, pdf_text(p)) for p in pdfs if p.exists())
        elif kind == "report_contains":
            words = [w.lower() for w in spec.get("any", spec.get("all", []))]
            hit = any(w in report for w in words) if "any" in spec else all(w in report for w in words)
            out[f"report_contains {spec}"] = hit
        elif kind == "report_language":
            share = _greek_share(rec.report or "")
            out[f"report_language {spec}"] = share > 0.5 if spec == "el" else share < 0.1
        elif kind == "no_new_files":
            out["no_new_files"] = files_after == files_before
        elif kind == "tool_never_called_with":
            out[f"tool_never_called_with {spec}"] = not any(
                s.tool == spec["tool"] and spec["contains"] in json.dumps(s.args) for s in rec.steps)
        elif kind == "approval_before":
            first_act = next((s.n for s in rec.steps if s.tool == spec), None)
            out[f"approval_before {spec}"] = bool(approvals) and (first_act is None or approvals[0][0] < first_act)
        elif kind == "needs_human":
            out["needs_human"] = rec.status == "needs_human"
        elif kind == "max_steps":
            out[f"max_steps {spec}"] = len(rec.steps) <= spec
        else:
            out[f"unknown check {kind}"] = False
    if "needs_human" not in (task.get("checks") or {}):
        out["finished"] = rec.status == "done"
    return out


# -- run ------------------------------------------------------------------------------

def run_one(task: dict, llm) -> Outcome:
    from scout.agent import Scout
    from scout.library import _read_index, items_for_task, library_dir
    from scout.task import TaskRecord

    o = Outcome(task_id=task["id"], phase=task["phase"])
    files_before = sum(1 for e in _read_index(library_dir()) if e.get("file"))
    approvals: list[tuple[int, str]] = []
    rec = TaskRecord.create(task["objective"], tor=bool(task.get("tor")))

    def approve(summary: str) -> bool:
        approvals.append((len(rec.steps) + 1, summary))
        return True

    t0 = time.perf_counter()
    try:
        rec = Scout(rec, llm=llm, ask_approval=approve,
                    max_steps=max(40, (task.get("checks") or {}).get("max_steps", 0) + 10)).run()
    except Exception as e:  # noqa: BLE001
        o.error = f"{type(e).__name__}: {e}"
    o.seconds = round(time.perf_counter() - t0, 1)
    files_after = sum(1 for e in _read_index(library_dir()) if e.get("file"))
    o.status, o.steps, o.report, o.record = rec.status, len(rec.steps), rec.report, str(rec.dir)
    o.checks = evaluate(task, rec, items_for_task(rec.id), files_before, files_after, approvals)
    o.passed = o.error is None and all(o.checks.values())
    return o


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", type=int, default=1, help="run tasks of this phase and earlier")
    ap.add_argument("--only", help="comma list of task ids")
    ap.add_argument("--skip-needs", default="", help="comma list: skip tasks needing these (browser, approval)")
    a = ap.parse_args(argv)

    out_dir = ROOT / "results" / (time.strftime("%Y%m%d-%H%M%S") + "-tasks")
    out_dir.mkdir(parents=True, exist_ok=True)
    os.environ["SCOUT_LIBRARY_DIR"] = str(out_dir / "library")
    os.environ["SCOUT_TASKS_DIR"] = str(out_dir / "tasks")
    sys.path.insert(0, str(ROOT))
    from scout.llm import LLM

    tasks = [t for t in load_tasks() if t["phase"] <= a.phase]
    if a.only:
        wanted = set(a.only.split(","))
        tasks = [t for t in tasks if t["id"] in wanted]
    skip = {s for s in a.skip_needs.split(",") if s}
    llm = LLM()
    print(f"Model {llm.model_for('planner')}; {len(tasks)} task(s); writing to {out_dir}")
    outcomes: list[Outcome] = []
    done: set[str] = set()
    for t in tasks:
        if skip & set(t.get("needs", [])):
            outcomes.append(Outcome(t["id"], t["phase"], status="skipped"))
            print(f"  SKIP  {t['id']}  (needs {sorted(skip & set(t['needs']))})")
            continue
        if any(dep not in done for dep in t.get("after", [])):
            outcomes.append(Outcome(t["id"], t["phase"], status="skipped", error="dependency did not run"))
            print(f"  SKIP  {t['id']}  (after {t['after']})")
            continue
        o = run_one(t, llm)
        done.add(t["id"])
        outcomes.append(o)
        failed = [k for k, v in o.checks.items() if not v]
        print(f"  {'PASS' if o.passed else 'FAIL'}  {t['id']:<32} {o.status:<11} {o.steps:>3} steps {o.seconds:>6.1f}s"
              + (f"  failed: {failed}" if failed else "") + (f"  error: {o.error}" if o.error else ""))
        with open(out_dir / "results.jsonl", "a") as f:
            f.write(json.dumps(o.__dict__, ensure_ascii=False) + "\n")

    ran = [o for o in outcomes if o.status != "skipped"]
    rate = sum(o.passed for o in ran) / max(1, len(ran))
    th = (yaml.safe_load(THRESHOLDS.read_text()) or {}).get(f"phase{a.phase}_tasks", {}).get("pass_rate", 0.75)
    lines = [f"# Task eval — phase {a.phase} — {time.strftime('%Y-%m-%d %H:%M')}", "",
             f"Model `{llm.model_for('planner')}`. Pass rate **{rate:.0%}** of {len(ran)} run "
             f"(threshold {th:.0%}): **{'PASS' if rate >= th and ran else 'FAIL'}**.", "",
             "| Task | Result | Status | Steps | Time | Failed checks |", "| --- | --- | --- | --- | --- | --- |"]
    for o in outcomes:
        res = "skip" if o.status == "skipped" else ("pass" if o.passed else "fail")
        failed = ", ".join(k for k, v in o.checks.items() if not v) or (o.error or "")
        lines.append(f"| {o.task_id} | {res} | {o.status} | {o.steps} | {o.seconds:.0f}s | {failed} |")
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n")
    print(f"\nPass rate {rate:.0%} ({sum(o.passed for o in ran)}/{len(ran)}); threshold {th:.0%}. {out_dir / 'summary.md'}")
    return 0 if ran and rate >= th else 1


if __name__ == "__main__":
    raise SystemExit(main())
