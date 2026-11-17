from __future__ import annotations

import httpx
import pytest

from scout.tools.datasets import (
    CKAN_PORTALS,
    DataFile,
    Dataset,
    search_ckan,
    search_datasets,
    search_zenodo,
)


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _handler(payload, requests=None, status=200):
    def handler(request):
        if requests is not None:
            requests.append(request)
        return httpx.Response(status, json=payload)

    return handler


def test_search_zenodo_maps_all_fields():
    payload = {
        "hits": {
            "total": 1,
            "hits": [
                {
                    "id": 123,
                    "doi": "10.5281/zenodo.123",
                    "links": {"self_html": "https://zenodo.org/records/123"},
                    "metadata": {
                        "title": "  <b>Dataset</b>  ",
                        "creators": [{"name": "Doe, Jane"}, {"name": "Other"}],
                        "publication_date": "2023-04-01",
                        "description": "<p>Text</p>",
                        "license": {"id": "cc-by-4.0"},
                    },
                    "files": [
                        {
                            "key": "data.csv",
                            "size": 2048,
                            "links": {
                                "self": "https://zenodo.org/api/records/123/files/data.csv/content"
                            },
                        }
                    ],
                }
            ],
        }
    }

    requests = []
    with _client(_handler(payload, requests)) as client:
        out = search_zenodo(client, "q")

    assert len(out) == 1
    d = out[0]
    assert d.title == "Dataset"
    assert d.url == "https://zenodo.org/records/123"
    assert d.description == "Text"
    assert d.publisher == "Doe, Jane"
    assert d.licence == "cc-by-4.0"
    assert d.year == 2023
    assert d.doi == "10.5281/zenodo.123"
    assert d.files == [
        DataFile(
            name="data.csv",
            url="https://zenodo.org/api/records/123/files/data.csv/content",
            format="CSV",
            size=2048,
        )
    ]
    assert d.source == "zenodo"
    assert requests[0].url.params["type"] == "dataset"


def test_search_zenodo_year_clause_only_with_year_from():
    requests = []
    payload = {"hits": {"hits": []}}

    with _client(_handler(payload, requests)) as client:
        search_zenodo(client, "climate", year_from=2020)
        search_zenodo(client, "climate")

    assert requests[0].url.params["q"] == "climate AND publication_date:[2020-01-01 TO *]"
    assert requests[1].url.params["q"] == "climate"


def test_search_zenodo_type_param():
    requests = []
    payload = {"hits": {"hits": []}}

    with _client(_handler(payload, requests)) as client:
        search_zenodo(client, "q", kind=None)
        search_zenodo(client, "q", kind="software")

    assert "type" not in requests[0].url.params
    assert requests[1].url.params["type"] == "software"


def test_search_zenodo_size_clamped():
    requests = []
    payload = {"hits": {"hits": []}}

    with _client(_handler(payload, requests)) as client:
        search_zenodo(client, "q", max_results=0)
        search_zenodo(client, "q", max_results=100)

    assert requests[0].url.params["size"] == "1"
    assert requests[1].url.params["size"] == "50"


def test_search_zenodo_missing_hits():
    with _client(_handler({})) as client:
        assert search_zenodo(client, "q") == []

    with _client(_handler({"hits": {}})) as client:
        assert search_zenodo(client, "q") == []


def test_search_ckan_maps_fields():
    payload = {
        "success": True,
        "result": {
            "count": 1,
            "results": [
                {
                    "name": "air-quality",
                    "title": "Air quality",
                    "notes": "Hourly <b>data</b>",
                    "organization": {"title": "Env Agency"},
                    "license_title": "CC-BY",
                    "metadata_created": "2021-06-01T10:00:00",
                    "resources": [
                        {
                            "name": "2021",
                            "url": "https://x/aq.csv",
                            "format": "csv",
                            "size": 1000,
                        }
                    ],
                }
            ],
        },
    }

    requests = []
    with _client(_handler(payload, requests)) as client:
        out = search_ckan(client, "air", base_url="https://example.org")

    assert len(out) == 1
    d = out[0]
    assert d.title == "Air quality"
    assert d.url == "https://example.org/dataset/air-quality"
    assert d.description == "Hourly data"
    assert d.publisher == "Env Agency"
    assert d.licence == "CC-BY"
    assert d.year == 2021
    assert d.files == [
        DataFile(name="2021", url="https://x/aq.csv", format="CSV", size=1000)
    ]
    assert d.source == "ckan:example.org"
    assert requests[0].url.path == "/api/3/action/package_search"
    assert requests[0].url.params["rows"] == "10"


def test_search_ckan_trailing_slash():
    requests = []
    payload = {"success": True, "result": {"results": []}}

    with _client(_handler(payload, requests)) as client:
        search_ckan(client, "q", base_url="https://example.org/")

    assert requests[0].url.path == "/api/3/action/package_search"
    assert requests[0].url.params["q"] == "q"


def test_search_ckan_success_false_raises():
    payload = {"success": False, "error": {"message": "bad"}}

    with _client(_handler(payload)) as client:
        with pytest.raises(RuntimeError, match="bad"):
            search_ckan(client, "q", base_url="https://example.org")


def test_search_ckan_resource_without_name_uses_url_file_and_uppercases_format():
    payload = {
        "success": True,
        "result": {
            "results": [
                {
                    "name": "n",
                    "title": "T",
                    "resources": [
                        {
                            "url": "https://x/path/aq.csv",
                            "format": "csv",
                            "size": "1000",
                        }
                    ],
                }
            ]
        },
    }

    with _client(_handler(payload)) as client:
        out = search_ckan(client, "q", base_url="https://example.org")

    f = out[0].files[0]
    assert f.name == "aq.csv"
    assert f.format == "CSV"
    assert f.size == 1000


def test_search_datasets_dispatch():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.host == "zenodo.org":
            return httpx.Response(200, json={"hits": {"hits": []}})
        return httpx.Response(200, json={"success": True, "result": {"results": []}})

    with _client(handler) as client:
        search_datasets(client, "q", "zenodo")
        search_datasets(client, "q", "canada")

    assert requests[0].url.host == "zenodo.org"
    assert requests[1].url.host == "open.canada.ca"
    assert requests[1].url.path == "/data/api/3/action/package_search"     # the Canada portal lives under /data


def test_search_datasets_unknown_source():
    with _client(_handler({})) as client:
        with pytest.raises(ValueError) as exc:
            search_datasets(client, "q", "nope")

    msg = str(exc.value)
    assert "zenodo" in msg
    for key in CKAN_PORTALS:
        assert key in msg


def test_dataset_to_dict_cuts_description_and_drops_empty():
    d = Dataset(title="T", url="https://x", description="x" * 500, files=[])
    out = d.to_dict(description_chars=10)

    assert out["description"] == "x" * 10 + "…"
    assert "publisher" not in out
    assert "licence" not in out
    assert "year" not in out
    assert "doi" not in out
    assert "files" not in out
    assert "source" not in out
    assert out["title"] == "T"
    assert out["url"] == "https://x"


def test_dataset_to_dict_keeps_non_empty_fields():
    d = Dataset(
        title="T",
        url="https://x",
        description="short",
        source="zenodo",
        files=[DataFile(name="a", url="https://x/a")],
    )
    out = d.to_dict(description_chars=10)

    assert out["description"] == "short"
    assert out["source"] == "zenodo"
    assert out["files"] == [{"name": "a", "url": "https://x/a", "format": None, "size": None}]


def test_search_ckan_multilingual_fields():
    """opendata.swiss returns per-language dicts for title, notes, organisation and resource names."""
    payload = {"success": True, "result": {"count": 1, "results": [{
        "name": "luftqualitaet", "title": {"de": "Luftqualität", "en": "Air quality", "fr": ""},
        "notes": {"de": "Stündlich", "en": "Hourly <b>data</b>"}, "organization": {"title": {"de": "BAFU", "en": "FOEN"}},
        "license_title": None, "metadata_created": "2022-01-01T00:00:00",
        "resources": [{"name": {"de": "Daten", "en": ""}, "url": "https://x/lq.csv", "format": "CSV"}]}]}}
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)))
    [d] = search_ckan(client, "air", base_url="https://ckan.opendata.swiss")
    assert d.title == "Air quality" and d.description == "Hourly data" and d.publisher == "FOEN"
    assert d.files[0].name == "Daten" and d.licence is None and d.source == "ckan:ckan.opendata.swiss"
