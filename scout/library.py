"""Hand-off to the knowledge library: the output contract.

Scout does not process what it collects; it drops each item into the library's
inbox for the library's own ingestion pipeline (design doc, "Integration with
the knowledge library"). One item = one folder:

    <SCOUT_LIBRARY_DIR>/<item id>/
        <item id>.md          Markdown with YAML front matter (the contract below), then notes/summary/text
        <original file>       the PDF, dataset or page snapshot, when there is one

Front matter fields: title, authors, year, doi, arxiv, source_url, date_retrieved,
task_id, licence, kind (paper | dataset | page | other), route (document |
video | audio), file, sha256, source (arxiv | openalex | crossref | web), plus
optional extra fields such as cited_by.

Deduplication: <inbox>/.scout-index.jsonl holds one line per item with its DOI,
arXiv id, normalised URL and file hash; Scout checks it before downloading.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import yaml

from dotenv import load_dotenv

from .config import ROOT

INDEX = ".scout-index.jsonl"
VIDEO_HOSTS = ("youtube.com", "youtu.be", "vimeo.com")
AUDIO_EXT = (".mp3", ".m4a", ".wav", ".ogg", ".flac")
VIDEO_EXT = (".mp4", ".mkv", ".webm", ".mov")


def library_dir() -> Path:
    load_dotenv(ROOT / ".env", override=False)
    d = Path(os.getenv("SCOUT_LIBRARY_DIR") or ROOT / "library-inbox").expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def norm_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    d = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", doi.strip(), flags=re.I).lower()
    return d or None


def norm_arxiv(arxiv: str | None) -> str | None:
    if not arxiv:
        return None
    a = re.sub(r"^(https?://arxiv\.org/(abs|pdf)/|arxiv:)", "", arxiv.strip(), flags=re.I)
    a = re.sub(r"(\.pdf)$", "", a)
    return re.sub(r"v\d+$", "", a).lower() or None      # versions count as the same paper


def norm_url(url: str | None) -> str | None:
    if not url:
        return None
    p = urlparse(url.strip())
    return f"{p.netloc.lower().removeprefix('www.')}{p.path.rstrip('/')}" + (f"?{p.query}" if p.query else "")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def route_for(url: str | None, filename: str | None) -> str:
    target = f"{url or ''} {filename or ''}".lower()
    if any(h in target for h in VIDEO_HOSTS) or target.rstrip().endswith(VIDEO_EXT):
        return "video"
    if target.rstrip().endswith(AUDIO_EXT):
        return "audio"
    return "document"


@dataclass
class Item:
    title: str
    source_url: str | None = None
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    doi: str | None = None
    arxiv: str | None = None
    kind: str = "other"
    source: str = "web"
    licence: str | None = None
    extra: dict = field(default_factory=dict)


def _slug(text: str, limit: int = 48) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:limit].strip("-") or "item"


def _read_index(lib: Path) -> list[dict]:
    p = lib / INDEX
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def find_existing(*, doi: str | None = None, arxiv: str | None = None, url: str | None = None,
                  sha256: str | None = None) -> dict | None:
    """The index entry of an item already in the library, matched by any identifier."""
    keys = {"doi": norm_doi(doi), "arxiv": norm_arxiv(arxiv), "url": norm_url(url), "sha256": sha256}
    for entry in _read_index(library_dir()):
        for k, v in keys.items():
            if v and entry.get(k) == v:
                return entry
    return None


def save_item(item: Item, *, task_id: str, body: str = "", file: Path | None = None) -> tuple[Path, bool]:
    """Write one item to the inbox. Returns (path to its .md, created).

    If the library already holds the same DOI, arXiv id, URL or file, nothing is
    written and the existing item's path is returned with created=False.
    """
    lib = library_dir()
    digest = sha256_file(file) if file else None
    existing = find_existing(doi=item.doi, arxiv=item.arxiv, url=item.source_url, sha256=digest)
    if existing:
        return lib / existing["path"], False

    item_id = f"{time.strftime('%Y%m%d')}-{_slug(item.title)}"
    folder = lib / item_id
    n = 2
    while folder.exists():
        folder = lib / f"{item_id}-{n}"
        n += 1
    folder.mkdir(parents=True)

    stored = None
    if file:
        stored = folder / file.name
        shutil.copy2(file, stored)

    meta = {
        "title": item.title,
        "authors": item.authors or None,
        "year": item.year,
        "doi": norm_doi(item.doi),
        "arxiv": norm_arxiv(item.arxiv),
        "source_url": item.source_url,
        "date_retrieved": time.strftime("%Y-%m-%d"),
        "task_id": task_id,
        "licence": item.licence,
        "kind": item.kind,
        "route": route_for(item.source_url, file.name if file else None),
        "source": item.source,
        "file": stored.name if stored else None,
        "sha256": digest,
        **item.extra,
    }
    meta = {k: v for k, v in meta.items() if v not in (None, [], "")}
    md = folder / f"{folder.name}.md"
    md.write_text("---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + "---\n\n"
                  + f"# {item.title}\n\n" + (body.strip() + "\n" if body.strip() else ""))

    entry = {"path": str(md.relative_to(lib)), "doi": meta.get("doi"), "arxiv": meta.get("arxiv"),
             "url": norm_url(item.source_url), "sha256": digest, "task_id": task_id, "title": item.title,
             "year": item.year, "source": item.source, "file": meta.get("file")}
    with open(lib / INDEX, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return md, True


def items_for_task(task_id: str) -> list[dict]:
    return [e for e in _read_index(library_dir()) if e.get("task_id") == task_id]


def read_front_matter(md: Path) -> dict:
    text = md.read_text()
    if not text.startswith("---\n"):
        return {}
    return yaml.safe_load(text.split("\n---\n", 1)[0][4:]) or {}
