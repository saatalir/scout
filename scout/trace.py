"""X2: an event trace per task, for debugging: `events.jsonl` in the task folder.

One JSON object per line, appended as things happen: every model turn (seconds, tokens, finish reason, which
tools it called), every tool call (ok, seconds, a short summary), Scout's notes to the user (reflections,
wrap-ups, verification, sessions) and the end of each run. The task record stays the source of truth for
resuming (task.json); this file answers "where did the time go, and what did the model see happen?".
Writing it never fails a task: errors are ignored.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

FILE = "events.jsonl"
_lock = threading.Lock()


def event(folder: Path, kind: str, **fields: Any) -> None:
    line = json.dumps({"t": round(time.time(), 3), "kind": kind, **fields}, ensure_ascii=False, default=str)
    try:
        with _lock:
            folder.mkdir(parents=True, exist_ok=True)
            with open(folder / FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError:
        pass


def read(folder: Path) -> list[dict]:
    try:
        lines = (folder / FILE).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue                      # a line cut by a crash
    return out


def summary(events: list[dict]) -> dict:
    """Where the time went: model vs tools, per tool, and tokens."""
    model = [e for e in events if e["kind"] == "model"]
    tools = [e for e in events if e["kind"] == "tool"]
    per_tool: dict[str, dict] = {}
    for e in tools:
        d = per_tool.setdefault(e.get("name", "?"), {"calls": 0, "failed": 0, "seconds": 0.0})
        d["calls"] += 1
        d["failed"] += not e.get("ok", True)
        d["seconds"] = round(d["seconds"] + (e.get("seconds") or 0), 1)
    return {"model_turns": len(model), "model_seconds": round(sum(e.get("seconds") or 0 for e in model), 1),
            "prompt_tokens": sum(e.get("prompt_tokens") or 0 for e in model),
            "completion_tokens": sum(e.get("completion_tokens") or 0 for e in model),
            "tool_calls": len(tools), "tool_seconds": round(sum(e.get("seconds") or 0 for e in tools), 1),
            "per_tool": dict(sorted(per_tool.items(), key=lambda kv: -kv[1]["seconds"]))}
