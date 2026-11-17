---
files: ["scout/tools/papers_extra.py", "tests/test_papers_extra.py"]
---
# A1 + A2: Semantic Scholar and PubMed adapters

Add two tier-1 academic sources in a NEW module `scout/tools/papers_extra.py`. Follow the style of
`scout/tools/academic.py` (read it first): each function takes an `httpx.Client` as its first argument and
returns `list[Paper]`, importing `Paper` and `_clean` from `scout.tools.academic`. Do not edit academic.py.
Never create an httpx client inside these functions. No API keys.

## A1 `search_semantic_scholar(client, query, *, year_from=None, max_results=10) -> list[Paper]`

Request: `GET https://api.semanticscholar.org/graph/v1/paper/search` with params
`query`, `limit` (= max_results clamped to 1..100),
`fields` = `title,authors,year,externalIds,url,openAccessPdf,abstract,venue,citationCount`,
and, only when year_from is given, `year` = f"{year_from}-".
Call `r.raise_for_status()`.

Response JSON: `{"total": 2, "offset": 0, "data": [ {...}, ... ]}`. Each item:
`{"paperId": "abc", "title": "...", "authors": [{"authorId": "1", "name": "A. B"}], "year": 2023,
  "externalIds": {"DOI": "10.1/x", "ArXiv": "2301.00001"}, "url": "https://www.semanticscholar.org/paper/abc",
  "openAccessPdf": {"url": "https://x/y.pdf", "status": "GREEN"} or null, "abstract": "..." or null,
  "venue": "..." or "", "citationCount": 12}`
`data` may be missing when there are no results: return [].

Map to Paper: title=_clean(title); authors=[a["name"]] (max 12); year; doi=externalIds.DOI (or None);
arxiv=externalIds.ArXiv (or None); url=item url; pdf_url=openAccessPdf.url when openAccessPdf is a dict;
abstract=_clean(abstract or ""); venue=venue or None; cited_by=citationCount; source="semantic_scholar".
Skip items without a title.

## A2 `search_pubmed(client, query, *, year_from=None, max_results=10) -> list[Paper]`

Two requests to NCBI E-utilities:
1. `GET https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi` params: `db=pubmed`, `term=query`,
   `retmode=json`, `retmax` (= max_results clamped 1..100), `sort=relevance`; when year_from is given also
   `datetype=pdat`, `mindate=str(year_from)`, `maxdate="3000"`.
   Response: `{"esearchresult": {"count": "2", "idlist": ["111", "222"]}}`. Empty idlist → return [] and do
   NOT make the second request.
2. `GET https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi` params: `db=pubmed`,
   `id=",".join(ids)`, `retmode=json`.
   Response: `{"result": {"uids": ["111", "222"], "111": {"uid": "111", "title": "A title.",
   "authors": [{"name": "Smith J", "authtype": "Author"}], "pubdate": "2023 Feb 1",
   "fulljournalname": "Journal X", "articleids": [{"idtype": "doi", "value": "10.1/abc"},
   {"idtype": "pmc", "value": "PMC123456"}, {"idtype": "pubmed", "value": "111"}]}, "222": {...}}}`

Map each uid, in `uids` order, to Paper: title=_clean(title).rstrip("."); authors=names (max 12);
year=int of the first 4 characters of pubdate when they are digits, else None; doi from articleids
idtype "doi" (else None); url=f"https://pubmed.ncbi.nlm.nih.gov/{uid}/";
pdf_url=f"https://europepmc.org/articles/{pmc}?pdf=render" when a "pmc" id exists, else None;
venue=fulljournalname or None; abstract=""; source="pubmed". Skip uids missing from result.

## Tests: `tests/test_papers_extra.py`

Use `httpx.Client(transport=httpx.MockTransport(handler))` with inline fixture JSON (no network).
Cover at least:
- semantic scholar: mapping of every field above; `year` param sent only with year_from; `limit` clamped
  (e.g. max_results=500 sends limit=100); null openAccessPdf → pdf_url None; missing `data` → [];
  item without title skipped.
- pubmed: two-step flow and field mapping (doi, pmc → europepmc pdf_url, year from "2023 Feb 1",
  title trailing dot removed, uids order kept); date params sent only with year_from; empty idlist → [] and
  only ONE request made (count requests in the handler); a non-numeric pubdate → year None.
- both: an HTTP 404 response raises httpx.HTTPStatusError.

Run `./run-tests tests/test_papers_extra.py` until it passes, then `./run-tests` (the whole suite must pass).
