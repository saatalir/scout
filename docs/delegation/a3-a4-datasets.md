---
files: ["scout/tools/datasets.py", "tests/test_datasets.py"]
context: ["scout/tools/academic.py"]
tests: ["tests/test_datasets.py"]
---
# A3 + A4: dataset sources (Zenodo, CKAN open-data portals)

Create a NEW module `scout/tools/datasets.py` for finding datasets. Follow the style of
`scout/tools/academic.py` (read it first): functions take an `httpx.Client` as first argument; never create
a client inside them; no API keys. Use `_clean` from `scout.tools.academic` to strip HTML.

## Data shape

```python
@dataclass
class DataFile:
    name: str
    url: str
    format: str | None = None      # upper-case, e.g. "CSV"
    size: int | None = None        # bytes

@dataclass
class Dataset:
    title: str
    url: str                       # landing page
    description: str = ""
    publisher: str | None = None
    licence: str | None = None
    year: int | None = None
    doi: str | None = None
    files: list[DataFile] = field(default_factory=list)
    source: str = ""               # "zenodo" or "ckan:<host>"

    def to_dict(self, description_chars: int = 400) -> dict: ...
        # like Paper.to_dict: asdict, description cut to description_chars with "…", drop None/""/[] values
```

## A3 `search_zenodo(client, query, *, year_from=None, max_results=10, kind="dataset") -> list[Dataset]`

`GET https://zenodo.org/api/records` with params `q` (= query, plus f" AND publication_date:[{year_from}-01-01 TO *]"
when year_from is given), `size` (= max_results clamped 1..50), `sort="bestmatch"`, and `type=kind` when kind
is not None. Call `raise_for_status()`.

Response: `{"hits": {"total": 1, "hits": [ {"id": 123, "doi": "10.5281/zenodo.123",
"links": {"self_html": "https://zenodo.org/records/123"}, "metadata": {"title": "T",
"creators": [{"name": "Doe, Jane"}], "publication_date": "2023-04-01", "description": "<p>Text</p>",
"license": {"id": "cc-by-4.0"}}, "files": [{"key": "data.csv", "size": 2048,
"links": {"self": "https://zenodo.org/api/records/123/files/data.csv/content"}}]} ]}}`

Map: title; url=links.self_html (fallback f"https://zenodo.org/records/{id}"); description=_clean(description);
publisher=first creator name or None; licence=license.id; year=int(publication_date[:4]) when digits;
doi; files=[DataFile(name=key, url=links.self, format=extension of key upper-cased or None, size)];
source="zenodo". Missing hits → [].

## A4 `search_ckan(client, query, *, base_url, max_results=10) -> list[Dataset]`

CKAN action API, used by many government open-data portals.
`GET {base_url.rstrip('/')}/api/3/action/package_search` params `q=query`, `rows` (clamped 1..50).
Response: `{"success": true, "result": {"count": 1, "results": [ {"name": "air-quality",
"title": "Air quality", "notes": "Hourly <b>data</b>", "organization": {"title": "Env Agency"},
"license_title": "CC-BY", "metadata_created": "2021-06-01T10:00:00", "resources": [{"name": "2021",
"url": "https://x/aq.csv", "format": "csv", "size": 1000}]} ]}}`
If `success` is false, raise `RuntimeError` with the response's `error` message.

Map: title; url=f"{base}/dataset/{name}"; description=_clean(notes); publisher=organization.title;
licence=license_title; year from metadata_created[:4]; files from resources (name or the URL's last path part,
url, format upper-cased or None, size as int or None); source=f"ckan:{host of base_url}".

Also define `CKAN_PORTALS = {"data.gov": "https://catalog.data.gov", "ireland": "https://data.gov.ie",
"switzerland": "https://ckan.opendata.swiss"}` and
`search_datasets(client, query, source="zenodo", *, year_from=None, max_results=10) -> list[Dataset]`:
`source` is "zenodo" or a key of CKAN_PORTALS (year_from applies to zenodo only); unknown source → ValueError
listing the valid ones.

## Tests: `tests/test_datasets.py`

`httpx.MockTransport` with inline fixtures (no network). Cover: zenodo mapping of every field; the year
clause added to `q` only with year_from; `type` param omitted when kind=None; size clamped; missing hits → [].
CKAN: mapping; base URL with a trailing slash; `success: false` → RuntimeError; resource without name uses the
URL's file name; format upper-cased. search_datasets dispatch and the ValueError. Dataset.to_dict cuts long
descriptions and drops empty fields.

The harness runs `tests/test_datasets.py` and sends you any failures.
