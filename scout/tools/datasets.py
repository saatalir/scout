"""Tier 1: dataset sources (Zenodo, CKAN open-data portals). No API keys.

Every source returns the same record shape (Dataset), so the planner can mix
them. The client is passed in, so the task's Tor choice applies (scout.net).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from urllib.parse import urlparse

import httpx

from scout.tools.academic import _clean

# Checked live 2026-11-17. catalog.data.gov no longer serves the standard CKAN action API (404), so it is not listed.
CKAN_PORTALS = {
    "canada": "https://open.canada.ca/data",
    "ireland": "https://data.gov.ie",
    "switzerland": "https://ckan.opendata.swiss",
}


def _text(value: object, lang: str = "en") -> str:
    """CKAN fields may be plain strings or per-language dicts ({"de": .., "en": ..}, as on opendata.swiss)."""
    if isinstance(value, dict):
        value = value.get(lang) or next((v for v in value.values() if v), "")
    return _clean(value if isinstance(value, str) else ("" if value is None else str(value)))


@dataclass
class DataFile:
    name: str
    url: str
    format: str | None = None
    size: int | None = None


@dataclass
class Dataset:
    title: str
    url: str
    description: str = ""
    publisher: str | None = None
    licence: str | None = None
    year: int | None = None
    doi: str | None = None
    files: list[DataFile] = field(default_factory=list)
    source: str = ""

    def to_dict(self, description_chars: int = 400) -> dict:
        d = asdict(self)
        d["description"] = (
            self.description[:description_chars] + "…"
            if len(self.description) > description_chars
            else self.description
        )
        return {k: v for k, v in d.items() if v not in (None, "", [])}


def _year_from_date(value: str | None) -> int | None:
    value = value or ""
    if len(value) >= 4 and value[:4].isdigit():
        return int(value[:4])
    return None


def _as_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _file_format(name: str | None) -> str | None:
    if not name or "." not in name:
        return None
    ext = name.rsplit(".", 1)[-1]
    return ext.upper() or None


def _last_path_part(url: str | None) -> str:
    if not url:
        return ""
    path = urlparse(url).path or url
    return path.rstrip("/").rsplit("/", 1)[-1]


def search_zenodo(
    client: httpx.Client,
    query: str,
    *,
    year_from: int | None = None,
    max_results: int = 10,
    kind: str | None = "dataset",
) -> list[Dataset]:
    q = query
    if year_from:
        q = f"{query} AND publication_date:[{year_from}-01-01 TO *]"

    params = {
        "q": q,
        "size": max(1, min(max_results, 50)),
        "sort": "bestmatch",
    }
    if kind is not None:
        params["type"] = kind

    r = client.get("https://zenodo.org/api/records", params=params)
    r.raise_for_status()

    hits = (r.json().get("hits") or {}).get("hits") or []
    out: list[Dataset] = []

    for hit in hits:
        meta = hit.get("metadata") or {}
        links = hit.get("links") or {}

        url = links.get("self_html") or f"https://zenodo.org/records/{hit.get('id')}"

        publisher = None
        for creator in meta.get("creators") or []:
            if isinstance(creator, dict):
                publisher = creator.get("name")
            else:
                publisher = str(creator)
            if publisher:
                break

        licence = None
        lic = meta.get("license")
        if isinstance(lic, dict):
            licence = lic.get("id")
        elif lic:
            licence = str(lic)

        files: list[DataFile] = []
        for f in hit.get("files") or []:
            key = f.get("key") or ""
            files.append(
                DataFile(
                    name=key,
                    url=(f.get("links") or {}).get("self") or "",
                    format=_file_format(key),
                    size=_as_int(f.get("size")),
                )
            )

        out.append(
            Dataset(
                title=_clean(meta.get("title")),
                url=url,
                description=_clean(meta.get("description")),
                publisher=publisher,
                licence=licence,
                year=_year_from_date(meta.get("publication_date")),
                doi=hit.get("doi") or meta.get("doi") or None,
                files=files,
                source="zenodo",
            )
        )

    return out


def search_ckan(
    client: httpx.Client,
    query: str,
    *,
    base_url: str,
    max_results: int = 10,
) -> list[Dataset]:
    base = base_url.rstrip("/")
    if "://" not in base:
        base = f"https://{base}"

    r = client.get(
        f"{base}/api/3/action/package_search",
        params={"q": query, "rows": max(1, min(max_results, 50))},
    )
    r.raise_for_status()

    data = r.json()
    if not data.get("success"):
        err = data.get("error")
        if isinstance(err, dict):
            message = err.get("message") or err.get("__type__") or str(err)
        else:
            message = err or "CKAN request failed"
        raise RuntimeError(message)

    host = urlparse(base).netloc or urlparse(base_url).netloc
    source = f"ckan:{host}"

    out: list[Dataset] = []
    for item in (data.get("result") or {}).get("results") or []:
        name = item.get("name") or ""
        org = item.get("organization") or {}
        publisher = (_text(org.get("title")) or None) if isinstance(org, dict) else None

        files: list[DataFile] = []
        for res in item.get("resources") or []:
            res_url = res.get("url") or ""
            res_name = _text(res.get("name")) or _last_path_part(res_url)
            fmt = res.get("format")
            files.append(
                DataFile(
                    name=res_name,
                    url=res_url,
                    format=str(fmt).upper() if fmt else None,
                    size=_as_int(res.get("size")),
                )
            )

        out.append(
            Dataset(
                title=_text(item.get("title")) or name,
                url=f"{base}/dataset/{name}",
                description=_text(item.get("notes")),
                publisher=publisher,
                licence=_text(item.get("license_title")) or None,
                year=_year_from_date(item.get("metadata_created")),
                files=files,
                source=source,
            )
        )

    return out


def search_datasets(
    client: httpx.Client,
    query: str,
    source: str = "zenodo",
    *,
    year_from: int | None = None,
    max_results: int = 10,
) -> list[Dataset]:
    if source == "zenodo":
        return search_zenodo(client, query, year_from=year_from, max_results=max_results)
    if source in CKAN_PORTALS:
        return search_ckan(client, query, base_url=CKAN_PORTALS[source], max_results=max_results)

    valid = ("zenodo", *CKAN_PORTALS)
    raise ValueError(f"unknown source {source!r}; use one of {', '.join(valid)}")
