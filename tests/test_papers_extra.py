from __future__ import annotations

import httpx
import pytest

from scout.tools.papers_extra import search_pubmed, search_semantic_scholar


def test_semantic_scholar_mapping_and_params():
    payload = {
        "total": 3,
        "offset": 0,
        "data": [
            {
                "paperId": "abc",
                "title": "  A   Title  ",
                "authors": [
                    {"authorId": "1", "name": "A. B"},
                    {"authorId": "2", "name": "C. D"},
                ],
                "year": 2023,
                "externalIds": {"DOI": "10.1/x", "ArXiv": "2301.00001"},
                "url": "https://www.semanticscholar.org/paper/abc",
                "openAccessPdf": {"url": "https://x/y.pdf", "status": "GREEN"},
                "abstract": "Some\n abstract",
                "venue": "Venue",
                "citationCount": 12,
            },
            {
                "paperId": "nooa",
                "title": "No OA",
                "authors": [],
                "year": None,
                "externalIds": {},
                "url": "https://www.semanticscholar.org/paper/nooa",
                "openAccessPdf": None,
                "abstract": None,
                "venue": "",
                "citationCount": 0,
            },
            {
                "paperId": "notitle",
                "title": "",
                "authors": [],
                "year": 2020,
                "externalIds": {},
                "url": "https://www.semanticscholar.org/paper/notitle",
                "openAccessPdf": None,
                "abstract": None,
                "venue": None,
                "citationCount": 0,
            },
        ],
    }

    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=payload)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        papers = search_semantic_scholar(client, "quantum", year_from=2020, max_results=5)

    assert len(requests) == 1
    params = requests[0].url.params
    assert params["query"] == "quantum"
    assert int(params["limit"]) == 5
    assert params["fields"] == (
        "title,authors,year,externalIds,url,openAccessPdf,abstract,venue,citationCount"
    )
    assert params["year"] == "2020-"

    assert len(papers) == 2

    p = papers[0]
    assert p.title == "A Title"
    assert p.authors == ["A. B", "C. D"]
    assert p.year == 2023
    assert p.doi == "10.1/x"
    assert p.arxiv == "2301.00001"
    assert p.url == "https://www.semanticscholar.org/paper/abc"
    assert p.pdf_url == "https://x/y.pdf"
    assert p.abstract == "Some abstract"
    assert p.venue == "Venue"
    assert p.cited_by == 12
    assert p.source == "semantic_scholar"

    p2 = papers[1]
    assert p2.title == "No OA"
    assert p2.authors == []
    assert p2.year is None
    assert p2.doi is None
    assert p2.arxiv is None
    assert p2.url == "https://www.semanticscholar.org/paper/nooa"
    assert p2.pdf_url is None
    assert p2.abstract == ""
    assert p2.venue is None
    assert p2.cited_by == 0
    assert p2.source == "semantic_scholar"


def test_semantic_scholar_year_param_only_with_year_from():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        search_semantic_scholar(client, "q")
        search_semantic_scholar(client, "q", year_from=2021)

    assert "year" not in requests[0].url.params
    assert requests[1].url.params["year"] == "2021-"


def test_semantic_scholar_limit_clamped():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        search_semantic_scholar(client, "q", max_results=500)
        search_semantic_scholar(client, "q", max_results=0)

    assert int(requests[0].url.params["limit"]) == 100
    assert int(requests[1].url.params["limit"]) == 1


def test_semantic_scholar_missing_data_returns_empty():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"total": 0, "offset": 0})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert search_semantic_scholar(client, "q") == []


def test_semantic_scholar_raises_on_404():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            search_semantic_scholar(client, "q")


def test_pubmed_two_step_mapping_and_params():
    esearch = {
        "esearchresult": {
            "count": "2",
            "idlist": ["111", "222"],
        }
    }
    esummary = {
        "result": {
            "uids": ["222", "111"],
            "111": {
                "uid": "111",
                "title": "A title.",
                "authors": [{"name": "Smith J", "authtype": "Author"}],
                "pubdate": "2023 Feb 1",
                "fulljournalname": "Journal X",
                "articleids": [
                    {"idtype": "doi", "value": "10.1/abc"},
                    {"idtype": "pmc", "value": "PMC123456"},
                    {"idtype": "pubmed", "value": "111"},
                ],
            },
            "222": {
                "uid": "222",
                "title": "Second title.",
                "authors": [{"name": "Doe J", "authtype": "Author"}],
                "pubdate": "Feb 1",
                "fulljournalname": "Journal Y",
                "articleids": [
                    {"idtype": "pubmed", "value": "222"},
                ],
            },
        }
    }

    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("esearch.fcgi"):
            return httpx.Response(200, json=esearch)
        return httpx.Response(200, json=esummary)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        papers = search_pubmed(client, "q", max_results=5)

    assert len(requests) == 2

    first = requests[0]
    assert first.url.path.endswith("esearch.fcgi")
    assert first.url.params["db"] == "pubmed"
    assert first.url.params["term"] == "q"
    assert first.url.params["retmode"] == "json"
    assert int(first.url.params["retmax"]) == 5
    assert first.url.params["sort"] == "relevance"
    assert "datetype" not in first.url.params
    assert "mindate" not in first.url.params
    assert "maxdate" not in first.url.params

    second = requests[1]
    assert second.url.path.endswith("esummary.fcgi")
    assert second.url.params["db"] == "pubmed"
    assert second.url.params["id"] == "111,222"
    assert second.url.params["retmode"] == "json"

    assert len(papers) == 2

    # uids order is kept, even though the result dict was inserted in another order
    assert papers[0].url == "https://pubmed.ncbi.nlm.nih.gov/222/"
    assert papers[1].url == "https://pubmed.ncbi.nlm.nih.gov/111/"

    p222 = papers[0]
    assert p222.title == "Second title"
    assert p222.authors == ["Doe J"]
    assert p222.year is None
    assert p222.doi is None
    assert p222.pdf_url is None
    assert p222.venue == "Journal Y"
    assert p222.abstract == ""
    assert p222.source == "pubmed"

    p111 = papers[1]
    assert p111.title == "A title"
    assert p111.authors == ["Smith J"]
    assert p111.year == 2023
    assert p111.doi == "10.1/abc"
    assert p111.pdf_url == "https://europepmc.org/articles/PMC123456?pdf=render"
    assert p111.venue == "Journal X"
    assert p111.abstract == ""
    assert p111.source == "pubmed"


def test_pubmed_date_params_only_with_year_from():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"esearchresult": {"count": "0", "idlist": []}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        search_pubmed(client, "q")
        search_pubmed(client, "q", year_from=2020)

    assert "datetype" not in requests[0].url.params
    assert "mindate" not in requests[0].url.params
    assert "maxdate" not in requests[0].url.params

    params = requests[1].url.params
    assert params["datetype"] == "pdat"
    assert params["mindate"] == "2020"
    assert params["maxdate"] == "3000"


def test_pubmed_empty_idlist_returns_empty_and_only_one_request():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"esearchresult": {"count": "0", "idlist": []}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert search_pubmed(client, "q") == []

    assert len(requests) == 1


def test_pubmed_retmax_clamped():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"esearchresult": {"count": "0", "idlist": []}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        search_pubmed(client, "q", max_results=500)
        search_pubmed(client, "q", max_results=0)

    assert int(requests[0].url.params["retmax"]) == 100
    assert int(requests[1].url.params["retmax"]) == 1


def test_pubmed_skips_missing_uid():
    esearch = {"esearchresult": {"count": "2", "idlist": ["1", "2"]}}
    esummary = {
        "result": {
            "uids": ["1", "2"],
            "1": {
                "uid": "1",
                "title": "Only one.",
                "authors": [],
                "pubdate": "2020",
                "fulljournalname": "Journal",
                "articleids": [],
            },
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("esearch.fcgi"):
            return httpx.Response(200, json=esearch)
        return httpx.Response(200, json=esummary)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        papers = search_pubmed(client, "q")

    assert len(papers) == 1
    assert papers[0].url == "https://pubmed.ncbi.nlm.nih.gov/1/"


def test_pubmed_raises_on_404():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            search_pubmed(client, "q")


def test_pubmed_raises_on_404_from_esummary():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("esearch.fcgi"):
            return httpx.Response(200, json={"esearchresult": {"count": "1", "idlist": ["1"]}})
        return httpx.Response(404, json={"error": "not found"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            search_pubmed(client, "q")
