"""X4: which Strata the results were measured on, and whether it changed since the last gate.

Strata runs on another machine, so its engine version is not visible over HTTP. What is visible identifies
what matters for results: the loaded model, its context, the parallel slots, the default sampling settings and
the chat template (`/props`, `/health`). A fingerprint of those, plus `STRATA_VERSION` from `.env` when the user
copies it from `strata --version`, is recorded with every eval result. `evals/strata_pin.json` holds the
fingerprint of the last passed gate; `scripts/check_connection.py` says when the server no longer matches it:
then rerun the gates before trusting results (design risk "Strata changes often").
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .config import ROOT

PIN = ROOT / "evals" / "strata_pin.json"
FIELDS = ("model", "n_ctx", "slots", "sampling", "template_sha256", "service", "version")


def fingerprint(base_url: str, api_key: str | None, *, get: Any = None) -> dict:
    """What identifies the running Strata, from /props and /health (no secrets, no hostnames)."""
    import httpx
    get = get or (lambda url, **kw: httpx.get(url, timeout=10, **kw))
    root = base_url.rstrip("/").removesuffix("/v1")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    props = get(root + "/props", headers=headers).json()
    health = get(root + "/health", headers=headers).json()
    gen = props.get("default_generation_settings") or {}
    template = props.get("chat_template") or ""
    return {"model": props.get("model_alias") or health.get("model"),
            "n_ctx": gen.get("n_ctx") or health.get("max_context"),
            "slots": props.get("total_slots"),
            "sampling": gen.get("params") or {},
            "template_sha256": hashlib.sha256(template.encode("utf-8")).hexdigest() if template else None,
            "service": health.get("service"),
            "version": os.getenv("STRATA_VERSION") or None}


def fingerprint_id(fp: dict) -> str:
    """A short id of a fingerprint, for result tables."""
    return hashlib.sha256(json.dumps({k: fp.get(k) for k in FIELDS}, sort_keys=True).encode()).hexdigest()[:12]


def current(settings: Any = None, *, get: Any = None) -> dict:
    from .config import load_settings
    s = settings or load_settings()
    return fingerprint(s.base_url, s.api_key, get=get)


def load_pin(path: Path = PIN) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def save_pin(fp: dict, gate: str, path: Path = PIN) -> None:
    """Record the fingerprint a gate passed on (the file is committed: it names no machine)."""
    data = {"gate": gate, "id": fingerprint_id(fp), "fingerprint": {k: fp.get(k) for k in FIELDS}}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    tmp.replace(path)


def differences(fp: dict, pin: dict | None) -> list[str]:
    """Which fields differ from the pin ([] when it matches; a missing pin is one difference). A version the
    pin did not record is not a difference (the user may add STRATA_VERSION later)."""
    if not pin:
        return ["no pin yet (evals/strata_pin.json): the next passed gate writes it"]
    old = pin.get("fingerprint") or {}
    out = []
    for k in FIELDS:
        if k == "version" and not old.get(k):
            continue
        if fp.get(k) != old.get(k):
            out.append(k if k == "template_sha256" else f"{k}: {old.get(k)!r} -> {fp.get(k)!r}")
    return out
