"""Tier 1: additional structured academic APIs (Semantic Scholar, PubMed). No API keys.

The client is passed in, so the task's Tor choice applies (scout.net).
"""

from __future__ import annotations

import httpx

from scout.tools.academic import Paper, _clean


def _author_names(authors: list | None) -> list[str]:
    names: list[str] = []
    for a in authors or []:
        name = a.get("name") if isinstance(a, dict) else a
        names.append("" if name is None else str(name))
        if len(names) >= 12:
            break
    return names


def search_semantic_scholar(client: httpx.Client, query: str, *, year_from: int | None = None,
                            max_results: int = 10) -> list[Paper]:
    params = {
        "query": query,
        "limit": max(1, min(max_results, 100)),
        "fields": "title,authors,year,externalIds,url,openAccessPdf,abstract,venue,citationCount",
    }
    if year_from is not None:
        params["year"] = f"{year_from}-"

    r = client.get("https://api.semanticscholar.org/graph/v1/paper/search", params=params)
    r.raise_for_status()

    out: list[Paper] = []
    for item in r.json().get("data") or []:
        title = _clean(item.get("title"))
        if not title:
            continue

        external = item.get("externalIds") or {}
        open_pdf = item.get("openAccessPdf")

        out.append(Paper(
            title=title,
            authors=_author_names(item.get("authors")),
            year=item.get("year"),
            doi=external.get("DOI") or None,
            arxiv=external.get("ArXiv") or None,
            url=item.get("url"),
            pdf_url=open_pdf.get("url") if isinstance(open_pdf, dict) else None,
            abstract=_clean(item.get("abstract") or ""),
            venue=item.get("venue") or None,
            cited_by=item.get("citationCount"),
            source="semantic_scholar",
        ))
    return out


def search_pubmed(client: httpx.Client, query: str, *, year_from: int | None = None,
                  max_results: int = 10) -> list[Paper]:
    params = {
        "db": "pubmed",
        "term": query,
        "retmode": "json",
        "retmax": max(1, min(max_results, 100)),
        "sort": "relevance",
    }
    if year_from is not None:
        params.update({
            "datetype": "pdat",
            "mindate": str(year_from),
            "maxdate": "3000",
        })

    r = client.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi", params=params)
    r.raise_for_status()

    ids = (r.json().get("esearchresult") or {}).get("idlist") or []
    if not ids:
        return []

    r = client.get(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
        params={
            "db": "pubmed",
            "id": ",".join(str(i) for i in ids),
            "retmode": "json",
        },
    )
    r.raise_for_status()

    result = r.json().get("result") or {}
    uids = result.get("uids") or ids

    out: list[Paper] = []
    for uid in uids:
        item = result.get(str(uid))
        if item is None:
            continue

        pubdate = str(item.get("pubdate") or "")
        year = int(pubdate[:4]) if len(pubdate) >= 4 and pubdate[:4].isdigit() else None

        doi = None
        pmc = None
        for article_id in item.get("articleids") or []:
            if not isinstance(article_id, dict):
                continue
            idtype = article_id.get("idtype")
            value = article_id.get("value")
            if idtype == "doi" and doi is None:
                doi = value or None
            elif idtype == "pmc" and pmc is None:
                pmc = value or None

        out.append(Paper(
            title=_clean(item.get("title")).rstrip("."),
            authors=_author_names(item.get("authors")),
            year=year,
            doi=doi,
            url=f"https://pubmed.ncbi.nlm.nih.gov/{uid}/",
            pdf_url=f"https://europepmc.org/articles/{pmc}?pdf=render" if pmc else None,
            abstract="",
            venue=item.get("fulljournalname") or None,
            source="pubmed",
        ))
    return out
