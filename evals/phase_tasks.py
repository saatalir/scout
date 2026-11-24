"""Run the fixed task eval set (evals/tasks/tasks.yaml) through the real agent and score it.

    python -m evals.phase_tasks                    # every phase-1 task
    python -m evals.phase_tasks --only r01-known-paper-pdf,r07-duplicate
    python -m evals.phase_tasks --skip-needs browser  # without the browser VM
    python -m evals.phase_tasks --phase 4 --phase-only --repeats 3   # gate G4: the open-ended tasks (rigor per task)

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
    repeat: int = 1
    rigor: str = "quick"


# -- checks ------------------------------------------------------------------------

def _greek_share(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    return sum("Ͱ" <= c <= "Ͽ" or "ἀ" <= c <= "῿" for c in letters) / max(1, len(letters))


CONFLICT_WORDS = ("contradict", "disagree", "conflict", "disput", "contested", "controvers", "critic", "debate",
                  "refut", "failed to replicate", "not replicated", "could not be replicated", "on the other side")


def _independent_sources(rec) -> int:
    from scout.independence import count_independent
    from scout.verify import gather_all_sources
    sources = gather_all_sources(rec.files, list(rec.state.get("evidence", [])))
    return count_independent([{"url": s["url"], "text": s["text"]} for s in sources])


def _confidence_rows(report: str) -> list[str]:
    """The confidence column of the findings table (RG5): 'high', 'medium', 'low' per row."""
    part = report.split("## findings and confidence", 1)
    if len(part) < 2:
        return []
    rows = [r for r in part[1].splitlines() if r.startswith("|") and not r.startswith("| finding")
            and not r.startswith("| ---")]
    out = []
    for r in rows:
        cells = [c.strip() for c in r.strip("|").split("|")]
        if len(cells) >= 2:
            out.append(cells[1].split(" ")[0])
    return [c for c in out if c in ("high", "medium", "low")]


def _campaign_ok(rec) -> bool:
    """G4's campaign test: it paused and resumed, the transcript was compacted, every session but the last added
    something (unless saturation ended it), and the campaign ended (finished, saturation or max sessions)."""
    c = rec.state.get("campaign") or {}
    done = [s for s in c.get("sessions", []) if "ended" in s]
    if len(done) < 2 or not (rec.dir / "sessions" / "transcript-1.json").exists():
        return False
    by_saturation = "nothing new" in (c.get("ended") or "")
    added = all(s["added"].get("evidence", 0) + s["added"].get("files", 0) > 0 for s in done[:-1])
    return (added or by_saturation) and (rec.status == "done" or bool(c.get("ended")))


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
        elif kind == "no_duplicate_items":
            keys = [k for m in metas for k in (m.get("doi"), m.get("arxiv"), m.get("source_url")) if k]
            out["no_duplicate_items"] = len(keys) == len(set(keys))
        elif kind == "max_steps":
            out[f"max_steps {spec}"] = len(rec.steps) <= spec
        # RG7: open-ended research (computed from the record and the report, never model-graded)
        elif kind == "report_has_facts":
            for words in spec:                     # each inner list: the report has at least one of them
                out[f"fact {words[0]!r}"] = any(w.lower() in report for w in words)
        elif kind == "min_independent_sources":
            out[f"min_independent_sources {spec}"] = _independent_sources(rec) >= spec
        elif kind == "findings_have_confidence":
            out["findings_have_confidence"] = bool(_confidence_rows(report))
        elif kind == "not_all_high":
            rows = _confidence_rows(report)
            out["not_all_high"] = bool(rows) and any(r != "high" for r in rows)
        elif kind == "counter_search_done":
            out["counter_search_done"] = any(s.get("purpose") == "counter" for s in rec.state.get("searches", []))
        elif kind == "report_mentions_conflict":
            out["report_mentions_conflict"] = any(w in report for w in CONFLICT_WORDS)
        elif kind == "campaign":
            out["campaign"] = _campaign_ok(rec)
        else:
            out[f"unknown check {kind}"] = False
    if "needs_human" not in (task.get("checks") or {}):
        # open-ended tasks may end on their budget with an honest, labelled report (R7)
        out["finished"] = rec.status == "done" or (bool(task.get("allow_partial")) and rec.status == "partial")
    return out


# -- run ------------------------------------------------------------------------------

def run_one(task: dict, llm, rigor: str | None = None) -> Outcome:
    from scout import campaign
    from scout.agent import Scout
    from scout.library import _read_index, items_for_task, library_dir
    from scout.task import TaskRecord

    o = Outcome(task_id=task["id"], phase=task["phase"], rigor=rigor or task.get("rigor") or "quick")
    approvals: list[tuple[int, str]] = []
    rec = TaskRecord.create(task["objective"], tor=bool(task.get("tor")))
    level = rigor or task.get("rigor")
    if level:
        rec.state["rigor"] = level
    sessions = task.get("sessions")
    if sessions:
        campaign.configure(rec.state, minutes=sessions["minutes"], max_sessions=sessions["n"])
    rec.save()

    def approve(summary: str) -> bool:
        approvals.append((len(rec.steps) + 1, summary))
        return True

    budget = max(40, (task.get("checks") or {}).get("max_steps", 0) + 10)
    env = {k: str(v) for k, v in (task.get("env") or {}).items()}
    if task.get("fresh_library"):
        # a task that must fetch an item itself, even if an earlier task in the run already saved it
        env["SCOUT_LIBRARY_DIR"] = str(library_dir().with_name(f"{library_dir().name}-{task['id']}"))
    saved_env = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    files_before = sum(1 for e in _read_index(library_dir()) if e.get("file"))
    t0 = time.perf_counter()
    try:
        if task.get("interrupt_after"):
            # Crash survival: stop after n steps (as if the process died), then resume from task.json alone.
            n = task["interrupt_after"]

            class Interrupted(Exception):
                pass

            def stop_after(msg: str) -> None:
                if msg.startswith(f"[{n}] "):
                    raise Interrupted
            try:
                Scout(rec, llm=llm, ask_approval=approve, max_steps=budget, notify=stop_after).run()
            except Interrupted:
                pass
            rec = TaskRecord.load(rec.id)
            o.status = f"interrupted at step {len(rec.steps)}"
        if sessions:
            # G4 campaign test: sessions until the campaign ends (max_sessions bounds it)
            for _ in range(sessions["n"] + 1):
                rec = Scout(TaskRecord.load(rec.id), llm=llm, ask_approval=approve, max_steps=budget,
                            session_minutes=sessions["minutes"]).run()
                if rec.status != "paused":
                    break
        else:
            rec = Scout(rec, llm=llm, ask_approval=approve, max_steps=budget).run()
    except Exception as e:  # noqa: BLE001
        o.error = f"{type(e).__name__}: {e}"
    o.seconds = round(time.perf_counter() - t0, 1)
    try:
        files_after = sum(1 for e in _read_index(library_dir()) if e.get("file"))
        o.status, o.steps, o.report, o.record = rec.status, len(rec.steps), rec.report, str(rec.dir)
        o.checks = evaluate(task, rec, items_for_task(rec.id), files_before, files_after, approvals)
    finally:                                   # the task's env (and library) stay in place for its checks
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    o.passed = o.error is None and all(o.checks.values())
    return o


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", type=int, default=1, help="run tasks of this phase and earlier")
    ap.add_argument("--phase-only", action="store_true", help="only the tasks of --phase itself")
    ap.add_argument("--rigor", choices=["quick", "standard", "thorough"],
                    help="run every task at this rigor (default: each task's own, else quick)")
    ap.add_argument("--only", help="comma list of task ids")
    ap.add_argument("--skip-needs", default="", help="comma list: skip tasks needing these (browser, approval)")
    ap.add_argument("--repeats", type=int, default=1,
                    help="run the whole set this many times, each repeat with its own library (the gate uses 3)")
    a = ap.parse_args(argv)

    out_dir = ROOT / "results" / (time.strftime("%Y%m%d-%H%M%S") + "-tasks")
    out_dir.mkdir(parents=True, exist_ok=True)
    os.environ["SCOUT_LIBRARY_DIR"] = str(out_dir / "library")
    os.environ["SCOUT_TASKS_DIR"] = str(out_dir / "tasks")
    sys.path.insert(0, str(ROOT))
    from scout.llm import LLM

    tasks = [t for t in load_tasks() if (t["phase"] == a.phase if a.phase_only else t["phase"] <= a.phase)]
    if a.only:
        wanted = set(a.only.split(","))
        tasks = [t for t in tasks if t["id"] in wanted]
    skip = {s for s in a.skip_needs.split(",") if s}
    llm = LLM()
    print(f"Model {llm.model_for('planner')}; {len(tasks)} task(s); writing to {out_dir}")
    outcomes: list[Outcome] = []
    for rep in range(1, a.repeats + 1):
        if a.repeats > 1:
            # each repeat starts from an empty library, so r07/r11 see the same situation every time
            os.environ["SCOUT_LIBRARY_DIR"] = str(out_dir / f"library-{rep}")
            os.environ["SCOUT_TASKS_DIR"] = str(out_dir / f"tasks-{rep}")
            print(f"Repeat {rep}/{a.repeats}")
        outcomes += run_set(tasks, llm, skip, out_dir, rep, a.rigor)
    return summarise(outcomes, a, llm, out_dir)


def run_set(tasks: list[dict], llm, skip: set[str], out_dir: Path, rep: int, rigor: str | None = None) -> list[Outcome]:
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
        o = run_one(t, llm, rigor)
        o.repeat = rep
        done.add(t["id"])
        outcomes.append(o)
        failed = [k for k, v in o.checks.items() if not v]
        print(f"  {'PASS' if o.passed else 'FAIL'}  {t['id']:<32} {o.status:<11} {o.steps:>3} steps {o.seconds:>6.1f}s"
              + (f"  failed: {failed}" if failed else "") + (f"  error: {o.error}" if o.error else ""))
        with open(out_dir / "results.jsonl", "a") as f:
            f.write(json.dumps(o.__dict__, ensure_ascii=False) + "\n")
    return outcomes


def _strata() -> tuple[dict | None, str]:
    from scout import strata_info
    try:
        fp = strata_info.current()
        return fp, strata_info.fingerprint_id(fp)
    except Exception as e:  # noqa: BLE001 - never fail an eval over its bookkeeping
        return None, f"unknown ({type(e).__name__})"


def summarise(outcomes: list[Outcome], a, llm, out_dir: Path) -> int:
    ran = [o for o in outcomes if o.status != "skipped"]
    rate = sum(o.passed for o in ran) / max(1, len(ran))
    th = (yaml.safe_load(THRESHOLDS.read_text()) or {}).get(f"phase{a.phase}_tasks", {}).get("pass_rate", 0.75)
    fp, fp_id = _strata()
    passed = bool(ran) and rate >= th
    lines = [f"# Task eval — phase {a.phase} — {time.strftime('%Y-%m-%d %H:%M')}", "",
             f"Model `{llm.model_for('planner')}`, Strata fingerprint `{fp_id}`. Pass rate **{rate:.0%}** of "
             f"{len(ran)} run (threshold {th:.0%}): **{'PASS' if passed else 'FAIL'}**.", "",
             "| Task | Result | Status | Steps | Time | Failed checks |", "| --- | --- | --- | --- | --- | --- |"]
    for o in outcomes:
        res = "skip" if o.status == "skipped" else ("pass" if o.passed else "fail")
        failed = ", ".join(k for k, v in o.checks.items() if not v) or (o.error or "")
        task = o.task_id + (f" (#{o.repeat})" if a.repeats > 1 else "")
        lines.append(f"| {task} | {res} | {o.status} | {o.steps} | {o.seconds:.0f}s | {failed} |")
    if a.repeats > 1:
        lines += ["", f"Per task over {a.repeats} repeats:", "", "| Task | Passed |", "| --- | --- |"]
        for tid in dict.fromkeys(o.task_id for o in ran):
            mine = [o for o in ran if o.task_id == tid]
            lines.append(f"| {tid} | {sum(o.passed for o in mine)}/{len(mine)} |")
    levels = sorted({o.rigor for o in ran})
    if levels:                                   # G4: what rigor costs
        lines += ["", "Cost per rigor level:", "", "| Rigor | Runs | Mean steps | Mean time | Passed |",
                  "| --- | --- | --- | --- | --- |"]
        for lv in levels:
            mine = [o for o in ran if o.rigor == lv]
            lines.append(f"| {lv} | {len(mine)} | {sum(o.steps for o in mine) / len(mine):.0f} | "
                         f"{sum(o.seconds for o in mine) / len(mine) / 60:.1f} min | {sum(o.passed for o in mine)}/{len(mine)} |")
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n")
    if fp:
        from scout import strata_info
        (out_dir / "strata.json").write_text(json.dumps({"id": fp_id, **fp}, indent=1) + "\n")
        if passed and a.repeats >= 3:            # a passed gate pins the Strata it passed on (X4)
            strata_info.save_pin(fp, gate=f"phase {a.phase} tasks, {time.strftime('%Y-%m-%d')}")
            print(f"Strata pinned: {strata_info.PIN} ({fp_id}); commit it with the results.")
    print(f"\nPass rate {rate:.0%} ({sum(o.passed for o in ran)}/{len(ran)}); threshold {th:.0%}. {out_dir / 'summary.md'}")
    return 0 if ran and rate >= th else 1


if __name__ == "__main__":
    raise SystemExit(main())
