"""Tier 1: structured academic APIs (arXiv, OpenAlex, Crossref). No API keys.

Every source returns the same record shape (Paper), so the planner can mix
them. The client is passed in, so the task's Tor choice applies (scout.net).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field

import httpx

SOURCES = ("arxiv", "openalex", "crossref")
ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


@dataclass
class Paper:
    title: str
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    doi: str | None = None
    arxiv: str | None = None
    url: str | None = None          # landing page
    pdf_url: str | None = None      # open-access PDF, when known
    abstract: str = ""
    venue: str | None = None
    cited_by: int | None = None
    source: str = ""

    def to_dict(self, abstract_chars: int = 400) -> dict:
        d = asdict(self)
        d["abstract"] = (self.abstract[:abstract_chars] + "…") if len(self.abstract) > abstract_chars else self.abstract
        return {k: v for k, v in d.items() if v not in (None, "", [])}


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


# -- arXiv ----------------------------------------------------------------------

def search_arxiv(client: httpx.Client, query: str, *, year_from: int | None = None, max_results: int = 10,
                 arxiv_id: str | None = None) -> list[Paper]:
    if arxiv_id:
        params = {"id_list": arxiv_id}
    else:
        # all: searches title, abstract and authors; quote multi-word phrases loosely
        params = {"search_query": "all:" + " AND all:".join(query.split()), "start": 0,
                  "max_results": max(1, min(max_results * (3 if year_from else 1), 100)),
                  "sortBy": "relevance"}
    r = client.get("https://export.arxiv.org/api/query", params=params)
    r.raise_for_status()
    root = ET.fromstring(r.text)
    out: list[Paper] = []
    for e in root.findall("a:entry", ATOM):
        abs_url = (e.findtext("a:id", "", ATOM) or "").strip()
        if "/abs/" not in abs_url:
            continue
        year = int((e.findtext("a:published", "", ATOM) or "0000")[:4]) or None
        if year_from and year and year < year_from:
            continue
        aid = re.sub(r"v\d+$", "", abs_url.split("/abs/")[-1])
        pdf = next((l.get("href") for l in e.findall("a:link", ATOM) if l.get("title") == "pdf"), None)
        out.append(Paper(
            title=_clean(e.findtext("a:title", "", ATOM)),
            authors=[_clean(a.findtext("a:name", "", ATOM)) for a in e.findall("a:author", ATOM)],
            year=year, doi=(e.findtext("arxiv:doi", None, ATOM) or None), arxiv=aid,
            url=f"https://arxiv.org/abs/{aid}", pdf_url=pdf or f"https://arxiv.org/pdf/{aid}",
            abstract=_clean(e.findtext("a:summary", "", ATOM)),
            venue=e.findtext("arxiv:journal_ref", None, ATOM), source="arxiv"))
        if len(out) >= max_results:
            break
    return out


# -- OpenAlex ---------------------------------------------------------------------

def _openalex_abstract(inv: dict | None) -> str:
    if not inv:
        return ""
    pos = sorted((i, w) for w, idx in inv.items() for i in idx)
    return " ".join(w for _, w in pos)


def search_openalex(client: httpx.Client, query: str, *, year_from: int | None = None, max_results: int = 10,
                    sort: str = "relevance") -> list[Paper]:
    # title_and_abstract.search, not the full-text `search` parameter: full text matches far too broadly
    # (sorted by citations, "retrieval-augmented generation" returned GPT-4 and SciPy).
    filters = ["type:article|preprint|review|book-chapter",
               "title_and_abstract.search:" + re.sub(r"[,:|]", " ", query).strip()]
    if year_from:
        filters.append(f"from_publication_date:{year_from}-01-01")
    params = {"per-page": max(1, min(max_results, 50)), "filter": ",".join(filters)}
    if sort == "cited":
        params["sort"] = "cited_by_count:desc"
    r = client.get("https://api.openalex.org/works", params=params)
    r.raise_for_status()
    out = []
    for w in r.json().get("results", []):
        doi = (w.get("doi") or "").removeprefix("https://doi.org/") or None
        loc = w.get("best_oa_location") or {}
        arxiv = None
        for l in w.get("locations") or []:
            m = re.search(r"arxiv\.org/(?:abs|pdf)/([\w.\-/]+?)(?:v\d+)?(?:\.pdf)?$", (l.get("landing_page_url") or ""))
            if m:
                arxiv = m.group(1)
                break
        out.append(Paper(
            title=_clean(w.get("title") or w.get("display_name")),
            authors=[(a.get("author") or {}).get("display_name", "") for a in (w.get("authorships") or [])][:12],
            year=w.get("publication_year"), doi=doi, arxiv=arxiv,
            url=(w.get("primary_location") or {}).get("landing_page_url") or (f"https://doi.org/{doi}" if doi else w.get("id")),
            pdf_url=loc.get("pdf_url"), abstract=_openalex_abstract(w.get("abstract_inverted_index")),
            venue=((w.get("primary_location") or {}).get("source") or {}).get("display_name"),
            cited_by=w.get("cited_by_count"), source="openalex"))
    return out


# -- Crossref ----------------------------------------------------------------------

def search_crossref(client: httpx.Client, query: str, *, year_from: int | None = None,
                    max_results: int = 10) -> list[Paper]:
    params = {"query.bibliographic": query, "rows": max(1, min(max_results, 50))}
    if year_from:
        params["filter"] = f"from-pub-date:{year_from}-01-01"
    r = client.get("https://api.crossref.org/works", params=params)
    r.raise_for_status()
    out = []
    for it in r.json().get("message", {}).get("items", []):
        parts = ((it.get("issued") or {}).get("date-parts") or [[None]])[0]
        pdf = next((l.get("URL") for l in it.get("link") or [] if "pdf" in (l.get("content-type") or "")), None)
        out.append(Paper(
            title=_clean((it.get("title") or [""])[0]),
            authors=[" ".join(x for x in (a.get("given"), a.get("family")) if x) for a in it.get("author") or []][:12],
            year=parts[0] if parts else None, doi=(it.get("DOI") or "").lower() or None,
            url=it.get("URL"), pdf_url=pdf, abstract=_clean(it.get("abstract")),
            venue=(it.get("container-title") or [None])[0], cited_by=it.get("is-referenced-by-count"),
            source="crossref"))
    return out


def search_papers(client: httpx.Client, query: str, source: str = "openalex", *, year_from: int | None = None,
                  max_results: int = 10, sort: str = "relevance", arxiv_id: str | None = None) -> list[Paper]:
    if source == "arxiv":
        return search_arxiv(client, query, year_from=year_from, max_results=max_results, arxiv_id=arxiv_id)
    if source == "openalex":
        return search_openalex(client, query, year_from=year_from, max_results=max_results, sort=sort)
    if source == "crossref":
        return search_crossref(client, query, year_from=year_from, max_results=max_results)
    raise ValueError(f"unknown source {source!r}; use one of {SOURCES}")
