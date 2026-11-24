"""RG8/RG9: research campaigns. One task that runs in sessions, each with its own time box.

A session ends paused with **progress notes** (what is covered, what is still needed, open leads, next
searches); the next session (`python -m scout resume <id> --session 2h`, by hand or from a schedule, RG10)
starts from a **compacted transcript**: the objective, the notes, the evidence and library indexes and the
searches already run (RG9). Nothing is lost: earlier transcripts are archived in the task folder
(`sessions/transcript-<n>.json`), and every session's notes in `sessions/<n>.md`.

A campaign ends when the model finishes (and the rigor rules accept it), when two sessions in a row add nothing
(no new evidence page, no new library item), at `until` (a date), after `max_sessions`, or when the user stops
it. Everything here is bookkeeping on the task record; the agent loop calls it (scout/agent.py).
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import date
from pathlib import Path
from typing import Any

SATURATION_SESSIONS = 2          # this many sessions in a row adding nothing end the campaign
LIST_SOURCES = 80                # sources listed in a compacted transcript
LIST_SEARCHES = 40
NOTES_CHARS = 6000

_DURATION_RE = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?$")


def parse_duration(text: str) -> int:
    """A session length in minutes: '2h' -> 120, '90m' -> 90, '1h30m' -> 90, '45' -> 45."""
    t = str(text or "").strip().lower()
    if t.isdigit():
        minutes = int(t)
    else:
        m = _DURATION_RE.match(t)
        if not t or not m or not (m.group(1) or m.group(2)):
            raise ValueError(f"session length {text!r}: use a form like 2h, 90m or 1h30m")
        minutes = int(m.group(1) or 0) * 60 + int(m.group(2) or 0)
    if not 0 < minutes <= 24 * 60:
        raise ValueError(f"session length {text!r}: must be more than 0 and at most 24h (like 2h, 90m or 1h30m)")
    return minutes


def parse_until(text: str) -> str:
    """A campaign end date, YYYY-MM-DD (the campaign runs no session after that day)."""
    try:
        return date.fromisoformat(str(text).strip()).isoformat()
    except ValueError:
        raise ValueError(f"--until {text!r}: use a date like 2026-12-31") from None


def campaign(state: dict) -> dict:
    return state.setdefault("campaign", {"sessions": [], "notes": "", "skipped": []})


def configure(state: dict, *, minutes: int | None = None, until: str | None = None,
              max_sessions: int | None = None) -> dict:
    """Set the campaign's limits (kept in the record, so a scheduled resume needs no flags but --session)."""
    c = campaign(state)
    if minutes:
        c["session_minutes"] = minutes
    if until:
        c["until"] = parse_until(until)
    if max_sessions is not None:
        if max_sessions < 1:
            raise ValueError("--max-sessions must be at least 1")
        c["max_sessions"] = max_sessions
    return c


def _counts(state: dict, files: list[str], steps: int) -> dict:
    return {"steps": steps, "evidence": len(state.get("evidence", [])), "files": len(files),
            "keys": len(state.get("seen_keys", [])), "searches": len(state.get("searches", []))}


def open_session(state: dict) -> dict | None:
    s = campaign(state)["sessions"]
    return s[-1] if s and "ended" not in s[-1] else None


def start_session(state: dict, *, files: list[str], steps: int, minutes: float) -> dict:
    """Open session n+1. A session still open (the process died) is closed first as interrupted."""
    c = campaign(state)
    if open_session(state) is not None:
        end_session(state, files=files, steps=steps, ended_by="interrupted")
    s = {"n": len(c["sessions"]) + 1, "started": time.time(), "minutes": minutes,
         "from": _counts(state, files, steps)}
    c["sessions"].append(s)
    return s


def end_session(state: dict, *, files: list[str], steps: int, ended_by: str, notes: str | None = None) -> dict:
    """Close the open session with what it added; keep the notes for the next one."""
    c = campaign(state)
    s = c["sessions"][-1]
    now = _counts(state, files, steps)
    s.update(ended=time.time(), ended_by=ended_by, to=now,
             added={k: now[k] - s["from"].get(k, 0) for k in now})
    if notes:
        c["notes"] = notes[:NOTES_CHARS]
    return s


def ended_reason(state: dict, today: date | None = None) -> str | None:
    """Why the campaign is over, from the record; None while it should go on."""
    c = campaign(state)
    done = [s for s in c["sessions"] if "ended" in s]
    if c.get("max_sessions") and len(done) >= c["max_sessions"]:
        return f"the campaign's {c['max_sessions']} session(s) are used"
    if c.get("until") and (today or date.today()) > date.fromisoformat(c["until"]):
        return f"the campaign's end date ({c['until']}) has passed"
    last = [s for s in done if s.get("ended_by") != "interrupted"][-SATURATION_SESSIONS:]
    if len(last) == SATURATION_SESSIONS and all(
            s["added"].get("evidence", 0) == 0 and s["added"].get("files", 0) == 0 for s in last):
        return f"the last {SATURATION_SESSIONS} sessions found nothing new (no new page read, no new item saved)"
    return None


def digest(s: dict, notes: str = "") -> str:
    """The report of a paused session: what it added, then its notes."""
    a = s.get("added", {})
    mins = (s.get("ended", time.time()) - s["started"]) / 60
    line = (f"**Session {s['n']}** ({mins:.0f} min, ended: {s.get('ended_by', '?')}): {a.get('steps', 0)} steps, "
            f"{a.get('searches', 0)} searches ({a.get('keys', 0)} new results), {a.get('evidence', 0)} new pages read, "
            f"{a.get('files', 0)} new library items.")
    return line + (f"\n\n{notes}" if notes else "")


def fallback_notes(state: dict, titles: list[str]) -> str:
    """Notes from the record alone, when the model wrote none (it was unreachable or the session crashed)."""
    searches = state.get("searches", [])[-10:]
    ev = state.get("evidence", [])[-10:]
    parts = ["## Notes (from the record; the model wrote none)"]
    if titles:
        parts.append("Library items so far: " + "; ".join(t[:80] for t in titles[-15:]))
    if ev:
        parts.append("Last pages read: " + "; ".join(f"{e.get('title') or e.get('url')}" for e in ev))
    if searches:
        parts.append("Last searches: " + "; ".join(f"{s['query']}" for s in searches))
    return "\n\n".join(parts)


def compacted(rec, *, titles: list[str], session_n: int) -> str:
    """RG9: the message that replaces earlier sessions' transcript: notes, sources read, items saved, searches."""
    st, c = rec.state, campaign(rec.state)
    prev = next((s for s in reversed(c["sessions"]) if "ended" in s), None)
    lines = [f"[Scout] Session {session_n} of this research campaign. Earlier sessions' transcripts are archived; "
             "this is what they established."]
    if prev:
        lines.append(f"The previous session ended: {prev.get('ended_by')}.")
    lines += ["", "## Progress notes from the previous session", c.get("notes") or "(none were written)"]
    ev = st.get("evidence", [])
    if ev:
        lines += ["", f"## Pages already read ({len(ev)}; kept as evidence — do not read them again)"]
        lines += [f"- {e.get('title') or '(untitled)'} — {e.get('url')}" for e in ev[-LIST_SOURCES:]]
    if titles:
        lines += ["", f"## Library items already saved ({len(titles)}; saving one again is skipped)"]
        lines += [f"- {t}" for t in titles[-LIST_SOURCES:]]
    searches = st.get("searches", [])
    if searches:
        lines += ["", f"## Searches already run ({len(searches)})"]
        lines += [f"- {'[counter] ' if s.get('purpose') == 'counter' else ''}{s['query']} ({s.get('new', 0)} new results)"
                  for s in searches[-LIST_SEARCHES:]]
    lines += ["", f"Today is {date.today().isoformat()}. Look for what is new or still missing: follow the open leads "
              "and next searches in the notes, phrase searches differently from the ones above, and look for newer "
              "material (add the year or 'latest'). Call finish only when the whole objective is done."]
    return "\n".join(lines)


def archive_transcript(rec, n: int) -> Path:
    """Keep the transcript before compaction: sessions/transcript-<n>.json (the session it belonged to)."""
    folder = rec.dir / "sessions"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"transcript-{n}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec.messages, ensure_ascii=False, indent=1))
    tmp.replace(path)
    return path


def write_notes(rec, s: dict, notes: str) -> Path:
    folder = rec.dir / "sessions"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{s['n']}.md"
    path.write_text(digest(s, notes) + "\n")
    return path


# -- running unattended (RG10) -----------------------------------------------------------------------

def acquire_lock(folder: Path) -> bool:
    """One session per task at a time: a lock file with the pid. A stale lock (its process is gone) is taken over."""
    folder.mkdir(parents=True, exist_ok=True)
    lock = folder / ".session.lock"
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                pid = int(lock.read_text().strip() or 0)
                os.kill(pid, 0)
                return False                     # alive: another session of this task is running
            except (ValueError, ProcessLookupError, PermissionError, OSError):
                lock.unlink(missing_ok=True)     # stale
                continue
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
        return True
    return False


def release_lock(folder: Path) -> None:
    lock = folder / ".session.lock"
    try:
        if lock.read_text().strip() == str(os.getpid()):
            lock.unlink()
    except OSError:
        pass


def skip(state: dict, reason: str) -> None:
    c = campaign(state)
    c["skipped"] = (c.get("skipped", []) + [{"at": time.time(), "reason": reason[:300]}])[-50:]


def blocked_reason(rec) -> str | None:
    """Why an unattended session must not start (it is skipped, and tried again next time)."""
    if rec.status in ("done", "failed"):
        return f"the task is {rec.status}; remove its schedule (python -m scout schedule --remove {rec.id})"
    if rec.status == "needs_human":
        return "the task waits for a person (a hand-off); run it by hand after doing what it asked"
    if campaign(rec.state).get("ended"):
        return f"the campaign has ended ({campaign(rec.state)['ended']})"
    if rec.tor is None:
        return "the Tor choice was never made for this task"
    return None


def preflight(rec, *, llm_factory: Any = None, host_check: Any = None) -> str | None:
    """R6 before an unattended session: the model and the browser host answer. Returns a problem, or None."""
    try:
        from .llm import LLM
        (llm_factory or LLM)().list_models()
    except Exception as e:  # noqa: BLE001
        return f"the model server did not answer ({type(e).__name__}: {str(e)[:120]})"
    try:
        (host_check or _browser_host_answers)(bool(rec.tor))
    except Exception as e:  # noqa: BLE001
        return f"the browser host did not answer ({type(e).__name__}: {str(e)[:160]})"
    return None


def _browser_host_answers(tor: bool) -> None:
    import asyncio
    from .browser import load_browser_config
    from .browser.hosts import run_helper
    cfg = load_browser_config()
    name = cfg.search.tor_profile if tor and cfg.search.tor_profile else cfg.search.profile
    profile = cfg.profile(name)
    if profile.host.kind == "local":
        return
    asyncio.run(run_helper(profile, "status", timeout=45))
