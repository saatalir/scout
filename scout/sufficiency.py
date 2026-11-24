"""RG3/RG4: when is open-ended research enough? Rules checked in code before a `thorough` task may finish.

The model proposes, code checks: everything here is computed from the task record (the searches it ran, the
sources it read, the corroboration of its report), never from what the model says about itself.

Rules (docs/RIGOR.md §3):
1. sites       — at least MIN_SITES independent sites read (same site or near-copies count once).
2. counter     — at least MIN_COUNTER searches with purpose "counter" (looking for disagreement).
3. saturation  — the last SATURATION_WINDOW searches found nothing new (no new site, paper or result).
4. corroborate — every finding is supported by at least two independent sources (checked last: it needs the
                 claim checker, about a minute).
Unsupported claims are handled by the claim check as before.
"""

from __future__ import annotations

from typing import Any

MIN_SITES = 5
MIN_COUNTER = 1
SATURATION_WINDOW = 2
MAX_REFUSALS = 2          # then the task finishes anyway, and the report says which rules were not met
LIST_MAX = 8


def search_keys(tool: str, result: Any) -> list[str]:
    """What a search found, as comparable keys: sites for web_search, identifiers for search_papers."""
    from .independence import registrable_domain
    if not isinstance(result, dict):
        return []
    keys = []
    for r in result.get("results") or []:
        if not isinstance(r, dict):
            continue
        if tool == "web_search":
            d = registrable_domain(r.get("url", ""))
            if d:
                keys.append(f"site:{d}")
        else:
            ident = r.get("doi") or r.get("arxiv_id") or r.get("arxiv") or (r.get("title") or "").strip().lower()
            if ident:
                keys.append(f"paper:{str(ident).lower()}")
    return keys


def log_search(state: dict, tool: str, query: str, purpose: str, result: Any) -> dict:
    """Record one search in the task state with how many of its results were new to the task."""
    seen = set(state.setdefault("seen_keys", []))
    keys = search_keys(tool, result)
    new = [k for k in dict.fromkeys(keys) if k not in seen]
    state["seen_keys"].extend(new)
    entry = {"tool": tool, "query": str(query)[:200], "purpose": purpose if purpose in ("main", "counter") else "main",
             "results": len(keys), "new": len(new)}
    state.setdefault("searches", []).append(entry)
    return entry


def process_misses(state: dict, sites: int) -> list[str]:
    """Rules 1–3: cheap, from the record. `sites` is the number of independent sites read."""
    misses = []
    if sites < MIN_SITES:
        misses.append(f"Breadth: you read {sites} independent site(s); read at least {MIN_SITES} (different "
                      "organisations, not copies of the same text).")
    searches = state.get("searches", [])
    counter = sum(s["purpose"] == "counter" for s in searches)
    if counter < MIN_COUNTER:
        misses.append("Disagreement: no counter-search yet. Run web_search or search_papers with purpose \"counter\" "
                      "to look for criticism, disagreement, failed replications or newer data that contradicts your "
                      "findings, and report any conflict with both sides' sources.")
    last = searches[-SATURATION_WINDOW:]
    if len(last) < SATURATION_WINDOW or any(s["new"] for s in last):
        misses.append(f"Saturation: your last {SATURATION_WINDOW} searches still turned up new sites or papers. "
                      "Search further (other wording, other angles) until new searches stop finding new material.")
    return misses


def corroboration_misses(claims: list[dict]) -> list[str]:
    """Rule 4: findings that rest on one independent source (or are contradicted without being addressed)."""
    single = [c for c in claims if c.get("verdict") != "unsupported" and c.get("independent", 0) < 2]
    if not single:
        return []
    listing = "\n".join(f"  - {c['claim'][:160]}" for c in single[:LIST_MAX])
    more = f"\n  (and {len(single) - LIST_MAX} more)" if len(single) > LIST_MAX else ""
    return [f"Corroboration: {len(single)} finding(s) rest on a single site. Look for a second, independent source "
            f"for the important ones (another organisation, not a copy); findings that stay single-source will be "
            f"marked unconfirmed:\n{listing}{more}"]


def summary(state: dict, sites: int, claims: list[dict] | None) -> str:
    """One line for the report: which rules were met when the task finished."""
    misses = process_misses(state, sites)
    if claims is not None:
        misses += corroboration_misses(claims)
    if not misses:
        return "_Thoroughness rules: all met._"
    names = [m.split(":", 1)[0] for m in misses]
    return (f"_Thoroughness rules not met: {', '.join(names)}. Scout sends a finish back at most {MAX_REFUSALS} times, "
            "and not when the budget is nearly used; treat this report as incomplete on those points._")
