"""Tier 2: fetch a page as Markdown, and download files with verification.

Both take an httpx client from scout.net, so the task's Tor choice applies.
A download only counts as done when verification passes (design doc,
"Verification: after a download"): the bytes are the kind of file claimed,
the size is plausible, and for a PDF with an expected title, the title is in
its first pages.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

# pypdf logs a warning per unusual font (e.g. LaTeX CFF fonts); text extraction still works.
logging.getLogger("pypdf").setLevel(logging.ERROR)

MAX_BYTES = 200 * 1024 * 1024
MIN_BYTES = 200
PAGE_CHARS = 8000          # Markdown handed to the model per fetch


@dataclass
class Page:
    url: str
    title: str
    markdown: str
    truncated: bool
    content_type: str
    chars: int

    def for_model(self) -> dict:
        return asdict(self)


def fetch_page(client: httpx.Client, url: str, *, max_chars: int = PAGE_CHARS) -> Page:
    import trafilatura

    r = client.get(url)
    r.raise_for_status()
    ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype in ("application/pdf",) or r.content[:5] == b"%PDF-":
        return Page(url=str(r.url), title="", markdown="This URL is a PDF file. Use download_file to fetch it.",
                    truncated=False, content_type="application/pdf", chars=0)
    if not ctype.startswith(("text/", "application/xhtml", "application/xml")) and ctype:
        return Page(url=str(r.url), title="", markdown=f"This URL is not a web page ({ctype}). Use download_file.",
                    truncated=False, content_type=ctype, chars=0)
    html = r.text
    md = trafilatura.extract(html, url=str(r.url), output_format="markdown", include_links=True,
                             include_tables=True, favor_recall=True) or ""
    if not md.strip():                                   # fall back to visible text
        md = re.sub(r"\s+", " ", re.sub(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", " ", html, flags=re.S | re.I)).strip()
    meta = trafilatura.extract_metadata(html, default_url=str(r.url))
    title = (meta.title if meta and meta.title else "") or _html_title(html)
    return Page(url=str(r.url), title=title.strip(), markdown=md[:max_chars], truncated=len(md) > max_chars,
                content_type=ctype or "text/html", chars=len(md))


def _html_title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


# -- downloads -------------------------------------------------------------------

@dataclass
class Download:
    url: str
    path: str | None
    bytes: int
    content_type: str
    kind: str                          # pdf | zip | gzip | csv | json | html | text | binary
    verified: bool
    sha256: str | None = None
    problems: list[str] = field(default_factory=list)
    pdf_pages: int | None = None
    pdf_title: str | None = None
    first_text: str = ""               # opening text of a PDF, to identify it

    def for_model(self) -> dict:
        d = asdict(self)
        d["first_text"] = d["first_text"][:400]
        return {k: v for k, v in d.items() if v not in (None, "", [])}


def sniff_kind(head: bytes, ctype: str) -> str:
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    if head.startswith(b"\x1f\x8b"):
        return "gzip"
    text = head[:2048].decode("utf-8", errors="ignore").lstrip().lower()
    if text.startswith(("<!doctype html", "<html")) or "<head" in text[:500] or ctype == "text/html":
        return "html"
    if text.startswith(("{", "[")):
        return "json"
    lines = [l for l in text.splitlines()[:5] if l.strip()]
    if lines and all("," in l for l in lines):
        return "csv"
    if head and all(32 <= b < 127 or b in (9, 10, 13) for b in head[:512]):
        return "text"
    return "binary"


EXT = {"pdf": ".pdf", "zip": ".zip", "gzip": ".gz", "csv": ".csv", "json": ".json", "html": ".html", "text": ".txt"}


def _filename(url: str, resp: httpx.Response, kind: str, wanted: str | None) -> str:
    name = wanted
    if not name:
        cd = resp.headers.get("content-disposition", "")
        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd, re.I)
        name = unquote(m.group(1)) if m else unquote(Path(urlparse(str(resp.url)).path).name)
    name = re.sub(r"[^\w.\-]+", "_", name or "").strip("._") or "download"
    if kind in EXT and not name.lower().endswith(EXT[kind]):
        name += EXT[kind]
    return name[:120]


def _words(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", s.lower())


def pdf_text(path: Path, pages: int = 2) -> str:
    """Title metadata plus the text of the first pages of a PDF file ("" if unreadable)."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        title = reader.metadata.title if reader.metadata and reader.metadata.title else ""
        body = " ".join((reader.pages[i].extract_text() or "") for i in range(min(pages, len(reader.pages))))
        return re.sub(r"\s+", " ", f"{title} {body}").strip()
    except Exception:  # noqa: BLE001
        return ""


def title_in_text(title: str, text: str, threshold: float = 0.8) -> bool:
    want = [w for w in _words(title) if len(w) > 2]
    have = set(_words(text))
    return bool(want) and sum(w in have for w in want) / len(want) >= threshold


def download_file(client: httpx.Client, url: str, dest_dir: Path, *, filename: str | None = None,
                  expected_title: str | None = None, expected_kind: str | None = None,
                  max_bytes: int = MAX_BYTES) -> Download:
    dest_dir.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    with client.stream("GET", url) as r:
        r.raise_for_status()
        ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
        declared = int(r.headers.get("content-length") or 0)
        if declared > max_bytes:
            return Download(url=url, path=None, bytes=declared, content_type=ctype, kind="binary", verified=False,
                            problems=[f"too large: {declared} bytes (limit {max_bytes})"])
        buf = io.BytesIO()
        for chunk in r.iter_bytes():
            buf.write(chunk)
            if buf.tell() > max_bytes:
                return Download(url=url, path=None, bytes=buf.tell(), content_type=ctype, kind="binary",
                                verified=False, problems=[f"too large: over {max_bytes} bytes"])
        data = buf.getvalue()
        final_url = str(r.url)
        kind = sniff_kind(data[:4096], ctype)
        name = _filename(final_url, r, kind, filename)

    if len(data) < MIN_BYTES:
        problems.append(f"too small: {len(data)} bytes")
    if kind == "html":
        problems.append("got an HTML page, not a file (a landing, login or paywall page?)")
    if expected_kind and expected_kind != "any" and kind != expected_kind:
        problems.append(f"expected a {expected_kind} file, got {kind}")

    pages = pdf_title = None
    first_text = ""
    if kind == "pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            pages = len(reader.pages)
            pdf_title = (reader.metadata.title if reader.metadata and reader.metadata.title else None)
            first_text = " ".join((reader.pages[i].extract_text() or "") for i in range(min(2, pages)))
            first_text = re.sub(r"\s+", " ", first_text).strip()
            if pages == 0:
                problems.append("PDF has no pages")
        except Exception as e:  # noqa: BLE001
            problems.append(f"PDF does not open: {type(e).__name__}")
        if expected_title and not title_in_text(expected_title, f"{pdf_title or ''} {first_text}"):
            problems.append(f"expected title not found in the PDF: {expected_title!r}")

    path = dest_dir / name
    n = 2
    while path.exists():
        path = dest_dir / f"{Path(name).stem}-{n}{Path(name).suffix}"
        n += 1
    path.write_bytes(data)
    return Download(url=final_url, path=str(path), bytes=len(data), content_type=ctype, kind=kind,
                    verified=not problems, sha256=hashlib.sha256(data).hexdigest(), problems=problems,
                    pdf_pages=pages, pdf_title=pdf_title, first_text=first_text[:1500])
