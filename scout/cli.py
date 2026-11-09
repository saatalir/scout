"""Command line: run, resume, show and list tasks.

    python -m scout run "Find 5 recent papers on hydrogen blending and download 2 PDFs"
    python -m scout run --tor "…"        # Tor on for this task (--no-tor: off)
    python -m scout list
    python -m scout show <task id>
    python -m scout resume <task id>

Tor (SCOUT_TOR): "ask" asks once at the start of the task; "on"/"off" decide;
--tor/--no-tor override. Approval gates ask on the terminal; without a
terminal every approval is denied.
"""

from __future__ import annotations

import argparse
import sys

from .browser import load_browser_config
from .library import library_dir
from .task import TaskRecord


def _yes(prompt: str) -> bool:
    if not sys.stdin.isatty():
        return False
    return input(prompt).strip().lower() in ("y", "yes", "on")


def decide_tor(flag: bool | None) -> bool:
    if flag is not None:
        return flag
    mode = load_browser_config().search.tor
    if mode in ("on", "off"):
        return mode == "on"
    if not sys.stdin.isatty():
        raise SystemExit("SCOUT_TOR=ask: pass --tor or --no-tor (no terminal to ask on).")
    return _yes("Use Tor for this task (searches and downloads)? [y/N] ")


def _approve(summary: str) -> bool:
    print(f"\n  APPROVAL NEEDED: {summary}")
    ok = _yes("  Approve? [y/N] ")
    print(f"  -> {'approved' if ok else 'denied'}\n")
    return ok


def _print_result(rec: TaskRecord) -> None:
    print(f"\nTask {rec.id}: {rec.status} after {len(rec.steps)} steps. Tor: {'on' if rec.tor else 'off'}.")
    if rec.files:
        print(f"Saved to {library_dir()}:")
        for f in rec.files:
            print(f"  - {f}")
    if rec.report:
        print("\n" + rec.report)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="scout", description="Online Scout: a local-first web research agent.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="run a new task")
    run.add_argument("objective")
    g = run.add_mutually_exclusive_group()
    g.add_argument("--tor", dest="tor", action="store_true", default=None)
    g.add_argument("--no-tor", dest="tor", action="store_false")
    run.add_argument("--max-steps", type=int, default=40)
    res = sub.add_parser("resume", help="continue a paused or unfinished task")
    res.add_argument("task_id")
    res.add_argument("--max-steps", type=int, default=40)
    show = sub.add_parser("show", help="print a task record")
    show.add_argument("task_id")
    sub.add_parser("list", help="list tasks")
    a = ap.parse_args(argv)

    if a.cmd == "list":
        for tid in TaskRecord.list_ids():
            r = TaskRecord.load(tid)
            print(f"{tid}  {r.status:<11}  {len(r.steps):>3} steps  {r.objective[:60]}")
        return 0
    if a.cmd == "show":
        r = TaskRecord.load(a.task_id)
        for s in r.steps:
            print(f"[{s.n:>2}] {s.tool:<16} {'ok ' if s.ok else 'ERR'} {s.seconds:>5.1f}s  {s.summary[:110]}")
        _print_result(r)
        return 0

    from .agent import Scout     # imported late: the model client is not needed for list/show
    if a.cmd == "run":
        rec = TaskRecord.create(a.objective, tor=decide_tor(a.tor))
    else:
        rec = TaskRecord.load(a.task_id)
    print(f"Task {rec.id} (Tor {'on' if rec.tor else 'off'})")
    rec = Scout(rec, ask_approval=_approve, notify=lambda m: print("  " + m), max_steps=a.max_steps).run()
    _print_result(rec)
    return 0 if rec.status == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
