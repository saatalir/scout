"""Command line: run, resume, show and list tasks.

    python -m scout run "Find 5 recent papers on hydrogen blending and download 2 PDFs"
    python -m scout run --tor "…"        # Tor on for this task (--no-tor: off)
    python -m scout run --where local "…"  # browsers on this machine, not the VM
    python -m scout list
    python -m scout show <task id>
    python -m scout resume <task id>
    python -m scout run --rigor thorough --session 2h "…"   # a campaign: sessions of 2 hours (RG8)
    python -m scout resume <task id> --session 2h          # the next session, from compacted notes (RG9)
    python -m scout schedule <task id> --daily 09:00 --session 2h   # a session every day (RG10)
    python -m scout schedule --list | --remove <task id>

Tor (SCOUT_TOR): "ask" asks once at the start of the task; "on"/"off" decide;
--tor/--no-tor override. --where overrides SCOUT_WHERE for this task. Approval
gates ask on the terminal; without a terminal every approval is denied.

A schedule is only ever installed by the user's own `schedule` command; Scout never
schedules itself. Scheduled sessions run `resume --unattended`: a session is skipped
(and tried next time) when the task is finished, waits for a person, is already
running, or the model or the browser host does not answer.
"""

from __future__ import annotations

import argparse
import os
import sys

from .browser import load_browser_config
from .config import ROOT
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


def _campaign_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--session", metavar="LENGTH",
                   help="run as a campaign session of this length (2h, 90m, 1h30m): it ends paused with progress notes")
    p.add_argument("--until", metavar="YYYY-MM-DD", help="campaign: no session after this date")
    p.add_argument("--max-sessions", type=int, metavar="N", help="campaign: end after N sessions")


def _schedule(a) -> int:
    from . import campaign
    from .schedule import ScheduleError, Scheduler, parse_time
    sched = Scheduler()
    try:
        if a.list:
            jobs = sched.list()
            for j in jobs:
                print(f"{j.task_id}  daily {j.hour:02d}:{j.minute:02d}  {' '.join(j.command[3:])}  ({j.where})")
            print(f"{len(jobs)} schedule(s).")
            return 0
        if not a.task_id:
            print("error: give a task id (or --list)", file=sys.stderr)
            return 2
        if a.remove:
            print("removed" if sched.remove(a.task_id) else "no schedule for that task")
            return 0
        if not a.daily or not a.session:
            print("error: --daily HH:MM and --session LENGTH are required", file=sys.stderr)
            return 2
        rec = TaskRecord.load(a.task_id)
        if rec.tor is None:
            print("error: the task has no Tor choice; run it once by hand first", file=sys.stderr)
            return 2
        hour, minute = parse_time(a.daily)
        campaign.configure(rec.state, minutes=campaign.parse_duration(a.session), until=a.until,
                           max_sessions=a.max_sessions)
        job = sched.install(a.task_id, hour, minute, a.session)
    except (ValueError, ScheduleError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    rec.save()
    print(f"scheduled: {a.task_id} daily at {hour:02d}:{minute:02d}, sessions of {a.session} ({job.where}). "
          f"Remove it with: python -m scout schedule --remove {a.task_id}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="scout", description="Online Scout: a local-first web research agent.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    where_help = "where the browsers run (default: SCOUT_WHERE, else vm)"
    rigor_help = ("how thoroughly to research: quick (concrete lookups), standard (sources kept, confidence shown), "
                  "thorough (corroboration and sufficiency checked before finishing); default: SCOUT_RIGOR, else quick")
    run = sub.add_parser("run", help="run a new task")
    run.add_argument("objective")
    g = run.add_mutually_exclusive_group()
    g.add_argument("--tor", dest="tor", action="store_true", default=None)
    g.add_argument("--no-tor", dest="tor", action="store_false")
    run.add_argument("--max-steps", type=int, default=40)
    run.add_argument("--approvals", choices=["ask", "auto", "deny"],
                     help="who approves irreversible actions (default: SCOUT_APPROVALS, else ask)")
    run.add_argument("--where", choices=["vm", "local"], default=None, help=where_help)
    run.add_argument("--rigor", choices=["quick", "standard", "thorough"], default=None, help=rigor_help)
    _campaign_args(run)
    res = sub.add_parser("resume", help="continue a paused or unfinished task")
    res.add_argument("task_id")
    res.add_argument("--max-steps", type=int, default=40)
    res.add_argument("--where", choices=["vm", "local"], default=None, help=where_help)
    res.add_argument("--rigor", choices=["quick", "standard", "thorough"], default=None,
                     help="change the task's rigor level (default: keep the one it started with)")
    _campaign_args(res)
    res.add_argument("--unattended", action="store_true",
                     help="for schedules: skip (and try next time) instead of running when the task cannot go on")
    sch = sub.add_parser("schedule", help="run a campaign's sessions every day (installed only by this command)")
    sch.add_argument("task_id", nargs="?")
    sch.add_argument("--daily", metavar="HH:MM")
    sch.add_argument("--session", metavar="LENGTH")
    sch.add_argument("--until", metavar="YYYY-MM-DD")
    sch.add_argument("--max-sessions", type=int, metavar="N")
    sch.add_argument("--list", action="store_true")
    sch.add_argument("--remove", action="store_true")
    show = sub.add_parser("show", help="print a task record")
    show.add_argument("task_id")
    sub.add_parser("list", help="list tasks")
    pro = sub.add_parser("promote", help="propose playbooks from flows that succeeded at least --min times (D4)")
    pro.add_argument("--min", type=int, default=2, dest="min_runs")
    a = ap.parse_args(argv)

    # Set right after parsing: everything below may load the browser config.
    if getattr(a, "where", None):
        os.environ["SCOUT_WHERE"] = a.where

    if a.cmd == "promote":
        from . import promote
        paths = promote.promote(min_runs=a.min_runs)
        for p in paths:
            print(f"proposed: {p}")
        print(f"{len(paths)} proposal(s)." + (" Review each, then move it up into the playbooks folder to activate it."
                                               if paths else " No flow has enough successful runs yet."))
        return 0

    if a.cmd == "schedule":
        return _schedule(a)

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
    from .evidence import normalize_rigor
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=False)
    # A new task takes --rigor, else SCOUT_RIGOR, else quick; a resumed one keeps its level unless --rigor is given.
    level = a.rigor or (os.environ.get("SCOUT_RIGOR") if a.cmd == "run" else None)
    try:
        rigor = normalize_rigor(level) if (level or a.cmd == "run") else None
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if a.cmd == "run":
        rec = TaskRecord.create(a.objective, tor=decide_tor(a.tor))
    else:
        rec = TaskRecord.load(a.task_id)
    if rigor:
        rec.state["rigor"] = rigor
        rec.save()
    from . import campaign
    try:
        minutes = campaign.parse_duration(a.session) if a.session else None
        if minutes or a.until or a.max_sessions is not None:
            campaign.configure(rec.state, minutes=minutes, until=a.until, max_sessions=a.max_sessions)
            rec.save()
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    # a campaign task keeps its session length: a plain `resume` runs its next session
    minutes = minutes or rec.state.get("campaign", {}).get("session_minutes")
    if minutes:
        if getattr(a, "unattended", False):
            why = campaign.blocked_reason(rec) or (None if campaign.acquire_lock(rec.dir) else
                                                   "another session of this task is running")
            if why is None:
                why = campaign.preflight(rec)
                if why:
                    campaign.release_lock(rec.dir)
            if why:
                campaign.skip(rec.state, why)
                rec.save()
                print(f"Task {rec.id}: session skipped: {why}")
                return 0
        elif not campaign.acquire_lock(rec.dir):
            print(f"error: another session of task {rec.id} is running", file=sys.stderr)
            return 1
    print(f"Task {rec.id} (Tor {'on' if rec.tor else 'off'}, rigor {rec.state.get('rigor', 'quick')})")
    from .approvals import ApprovalPolicy
    policy = ApprovalPolicy.from_env(getattr(a, "approvals", None))
    if policy.mode != "ask":
        print(f"Approvals: {policy.mode}" + (" (no human in the loop)" if policy.mode == "auto" else " (dry run)"))
    try:
        rec = Scout(rec, ask_approval=_approve, notify=lambda m: print("  " + m), max_steps=a.max_steps,
                    approvals=policy, session_minutes=minutes).run()
    finally:
        if minutes:
            campaign.release_lock(rec.dir)
    _print_result(rec)
    return 0 if rec.status in ("done", "paused") else 1


if __name__ == "__main__":
    raise SystemExit(main())
