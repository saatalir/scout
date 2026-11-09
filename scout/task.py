"""The task record: everything Scout knows about one task, saved after every step.

A task can be paused (at an approval gate, or by the human) and resumed later
from its record. Layout under SCOUT_TASKS_DIR (default ./tasks, git-ignored):

    tasks/<task id>/task.json      this record
    tasks/<task id>/downloads/     files fetched for the task, before they go to the library
"""

from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from .config import ROOT

STATUSES = ("running", "paused", "done", "failed", "needs_human")


def tasks_dir() -> Path:
    load_dotenv(ROOT / ".env", override=False)
    return Path(os.getenv("SCOUT_TASKS_DIR") or ROOT / "tasks").expanduser()


def new_task_id(objective: str) -> str:
    words = "".join(c if c.isalnum() else " " for c in objective.lower()).split()
    slug = "-".join(w for w in words if w.isascii())[:32].strip("-") or "task"
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}-{secrets.token_hex(2)}"


@dataclass
class Step:
    n: int
    tool: str
    args: dict
    ok: bool
    summary: str                   # what the tool returned, shortened
    seconds: float = 0.0
    at: float = field(default_factory=time.time)


@dataclass
class TaskRecord:
    id: str
    objective: str
    tor: bool | None = None        # the user's Tor answer for this task
    status: str = "running"
    plan: list[str] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    files: list[str] = field(default_factory=list)       # library items this task saved (paths to their .md)
    failures: list[str] = field(default_factory=list)    # what did not work, so it is not retried blindly
    pending_approval: str | None = None                  # the action waiting at a gate
    report: str = ""
    messages: list[dict] = field(default_factory=list)   # model conversation, for resuming
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)

    # -- paths ---------------------------------------------------------------
    @property
    def dir(self) -> Path:
        return tasks_dir() / self.id

    @property
    def downloads(self) -> Path:
        d = self.dir / "downloads"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # -- lifecycle -------------------------------------------------------------
    @classmethod
    def create(cls, objective: str, tor: bool | None = None) -> "TaskRecord":
        rec = cls(id=new_task_id(objective), objective=objective.strip(), tor=tor)
        rec.save()
        return rec

    def log(self, tool: str, args: dict, ok: bool, summary: str, seconds: float = 0.0) -> Step:
        step = Step(n=len(self.steps) + 1, tool=tool, args=args, ok=ok, summary=summary[:500], seconds=round(seconds, 2))
        self.steps.append(step)
        if not ok:
            self.failures.append(f"{tool}({json.dumps(args, ensure_ascii=False)[:200]}): {summary[:200]}")
        self.save()
        return step

    def save(self) -> None:
        self.updated = time.time()
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / "task.json.tmp"
        tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=1))
        tmp.replace(self.dir / "task.json")       # atomic: a crash never leaves half a record

    @classmethod
    def load(cls, task_id: str) -> "TaskRecord":
        raw = json.loads((tasks_dir() / task_id / "task.json").read_text())
        raw["steps"] = [Step(**s) for s in raw.get("steps", [])]
        return cls(**raw)

    @staticmethod
    def list_ids() -> list[str]:
        d = tasks_dir()
        return sorted(p.name for p in d.iterdir() if (p / "task.json").exists()) if d.exists() else []
