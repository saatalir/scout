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
    return safe_name(name, kind)


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
    return verify_bytes(data, final_url, ctype, name, dest_dir, kind=kind, expected_title=expected_title,
                        expected_kind=expected_kind)


def safe_name(name: str | None, kind: str) -> str:
    name = re.sub(r"[^\w.\-]+", "_", name or "").strip("._") or "download"
    if kind in EXT and not name.lower().endswith(EXT[kind]):
        name += EXT[kind]
    return name[:120]


def verify_bytes(data: bytes, url: str, ctype: str, name: str, dest_dir: Path, *, kind: str | None = None,
                 expected_title: str | None = None, expected_kind: str | None = None) -> Download:
    """The checks every download passes, whichever way it arrived (HTTP or the real browser): size, kind
    from the first bytes, not an HTML page, PDF opens and contains the expected title. Writes the file."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    kind = kind or sniff_kind(data[:4096], ctype)
    final_url = url
    if len(data) > MAX_BYTES:
        return Download(url=url, path=None, bytes=len(data), content_type=ctype, kind="binary", verified=False,
                        problems=[f"too large: over {MAX_BYTES} bytes"])
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


# -- alternative PDF locations (R2: alternatives before giving up) ---------------------------------

def _landing_pdf_links(html: str, base: str) -> list[str]:
    """PDF links a publisher page advertises: the citation_pdf_url meta tag first, then .pdf / /pdf/ links."""
    from urllib.parse import urljoin
    out = [m.group(1) for m in re.finditer(
        r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)', html, re.I)]
    out += [m.group(1) for m in re.finditer(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']citation_pdf_url["\']', html, re.I)]
    for m in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\']', html, re.I):
        href = m.group(1)
        if re.search(r"\.pdf($|[?#])|/pdf(/|$)", href, re.I):
            out.append(href)
    seen, links = set(), []
    for h in out:
        u = urljoin(base, h.replace("&amp;", "&"))
        if u.startswith("http") and u not in seen:
            seen.add(u)
            links.append(u)
    return links[:5]


def _lookup_title(client: httpx.Client, title: str) -> tuple[str | None, str | None, str | None]:
    """(doi, arxiv id, open-access pdf url) of the work with this title, from OpenAlex; best effort."""
    q = re.sub(r"[,:|]", " ", title).strip()
    r = client.get("https://api.openalex.org/works", params={"filter": f"title.search:{q}", "per-page": 5})
    if r.status_code != 200:
        return None, None, None
    for w in r.json().get("results", []):
        if not title_in_text(title, w.get("title") or "", 0.9) or not title_in_text(w.get("title") or "", title, 0.9):
            continue
        doi = (w.get("doi") or "").removeprefix("https://doi.org/") or None
        arxiv = None
        for l in w.get("locations") or []:
            m = re.search(r"arxiv\.org/(?:abs|pdf)/([\w.\-/]+?)(?:v\d+)?(?:\.pdf)?$", l.get("landing_page_url") or "")
            if m:
                arxiv = m.group(1)
                break
        return doi, arxiv, (w.get("best_oa_location") or {}).get("pdf_url")
    return None, None, None


def pdf_candidates(client: httpx.Client, failed_url: str, *, doi: str | None = None, arxiv: str | None = None,
                   landing_url: str | None = None, title: str | None = None) -> list[str]:
    """Other places the same PDF may be, most reliable first. Each lookup is best effort.
    With neither DOI nor arXiv id, the expected title is looked up in OpenAlex to find them."""
    cands: list[str] = []
    if title and not (doi or arxiv):
        try:
            doi, arxiv, oa = _lookup_title(client, title)
            cands += [oa] if oa else []
        except Exception:  # noqa: BLE001
            pass
    if arxiv:
        cands.append(f"https://arxiv.org/pdf/{re.sub(r'v\\d+$', '', arxiv.strip())}")
    if doi:
        try:
            r = client.get(f"https://api.openalex.org/works/doi:{doi.strip()}")
            if r.status_code == 200:
                w = r.json()
                best = (w.get("best_oa_location") or {}).get("pdf_url")
                cands += [best] if best else []
                cands += [l.get("pdf_url") for l in w.get("locations") or [] if l.get("pdf_url")]
        except Exception:  # noqa: BLE001 - one lookup failing must not stop the others
            pass
    for page in [p for p in (landing_url, failed_url, f"https://doi.org/{doi}" if doi else None) if p]:
        try:
            r = client.get(page)
            if r.status_code == 200 and "html" in r.headers.get("content-type", ""):
                cands += _landing_pdf_links(r.text, str(r.url))
                break
        except Exception:  # noqa: BLE001
            continue
    seen, out = {failed_url}, []
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out[:6]
