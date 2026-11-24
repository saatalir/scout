"""RG1: the evidence store. Pages Scout reads count as sources, without going into the library.

At rigor `standard` and `thorough` (docs/RIGOR.md), every page read with `fetch_page`, `browser_open` or
`browser_read` is kept in the task folder as `evidence/<n>.md` (front matter: url, title, via, fetched_at,
sha256, chars; then the page text) and indexed in `rec.state["evidence"]`, so the record alone says what the
report can rest on and a resumed task keeps it. The library is unchanged: only what the objective asks for is
saved there. Pages with the same text are stored once (by sha256 of the text).

At `quick`, nothing is stored: today's behaviour.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RIGOR_LEVELS = ("quick", "standard", "thorough")
MIN_CHARS = 200              # shorter texts (error pages, "this URL is a PDF") are not evidence
MAX_CHARS = 200_000          # a page's text is kept up to this size


def normalize_rigor(value: Any) -> str:
    """A rigor level from the CLI or SCOUT_RIGOR; empty means quick. Raises ValueError naming the fix."""
    v = str(value or "quick").strip().lower()
    if v not in RIGOR_LEVELS:
        raise ValueError(f"rigor must be one of {RIGOR_LEVELS}, got {v!r} (check SCOUT_RIGOR in .env or --rigor)")
    return v


def index(state: dict) -> list[dict]:
    return state.setdefault("evidence", [])


def add(state: dict, folder: Path, *, url: str, title: str, text: str, via: str) -> dict | None:
    """Keep one page as evidence; returns its index entry (an existing one for the same text), or None when the
    text is too short to be evidence."""
    text = (text or "").strip()
    if len(text) < MIN_CHARS:
        return None
    text = text[:MAX_CHARS]
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    items = index(state)
    for item in items:
        if item["sha256"] == digest:
            return item
    n = max((i["n"] for i in items), default=0) + 1
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{n}.md"
    entry = {"n": n, "url": url, "title": (title or "")[:300], "via": via,
             "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "sha256": digest, "chars": len(text), "path": str(path)}
    head = "\n".join(f"{k}: {json.dumps(entry[k], ensure_ascii=False)}" for k in
                     ("url", "title", "via", "fetched_at", "sha256", "chars"))
    tmp = path.with_suffix(".tmp")
    tmp.write_text(f"---\n{head}\n---\n\n{text}\n", encoding="utf-8")
    tmp.replace(path)
    items.append(entry)
    return entry


def text_of(entry: dict) -> str:
    """The stored page text (without the front matter); '' if the file is gone."""
    try:
        raw = Path(entry["path"]).read_text(encoding="utf-8")
    except OSError:
        return ""
    if raw.startswith("---\n"):
        end = raw.find("\n---\n", 4)
        if end != -1:
            return raw[end + 5:].strip()
    return raw.strip()


def merge(state: dict, entries: list[dict]) -> int:
    """Add a sub-agent's evidence entries to the parent's index (same files, no copy); returns how many were new."""
    items = index(state)
    known = {i["sha256"] for i in items}
    added = 0
    for e in entries:
        if e.get("sha256") in known:
            continue
        n = max((i["n"] for i in items), default=0) + 1
        items.append({**e, "n": n})
        known.add(e["sha256"])
        added += 1
    return added
