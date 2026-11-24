"""Research verification (design doc: "After research: check every claim in a summary against a stored
source file").

Before a task's report is accepted, its factual claims are checked against the
text of the items the task saved: each item's notes plus the opening of its
stored PDF. One call to the "extractor" role returns, per claim, the item it
relies on, a verdict (supported / partial / unsupported) and a short quote.
Unsupported claims go back to the planner to fix; the check runs once per task
and never blocks finishing if it fails itself. It runs with the model's
reasoning off (a claim-by-claim comparison needs no deliberation): about 50 s
instead of 5 minutes on a long report.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .library import read_front_matter
from .tools.web import pdf_text

SOURCE_CHARS = 3500          # text per source handed to the checker
MAX_SOURCES = 12
META_FIELDS = ("authors", "year", "doi", "arxiv", "source_url", "licence", "cited_by", "kind")

PROMPT = """You check a research report against the sources it was written from.

List the factual claims in the REPORT about content: findings, numbers, conclusions, what a source says.
Skip: checklist lines; what the agent did or saved; bibliographic details (titles, authors, years, venues,
DOIs, arXiv ids, citation counts, licences: they were copied from the catalogues). At most 20 claims; merge
claims that say the same thing.
For each claim, decide using ONLY the SOURCES below:
- "supported": a source states it,
- "partial": a source states part of it, or something close but not the same,
- "unsupported": no source states it.

Reply with compact JSON only (one claim per line, evidence at most 12 words), no other text:
{"claims": [
{"claim": "...", "source": "S1", "verdict": "supported|partial|unsupported", "evidence": "short quote or empty"}
]}
If the report makes no such claims, reply {"claims": []}.

REPORT:
<<<
%s
>>>

SOURCES:
%s"""


@dataclass
class Verification:
    checked: int = 0
    unsupported: list[dict] = field(default_factory=list)
    partial: list[dict] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and not self.unsupported

    def note(self) -> str:
        if self.error:
            return f"_Not verified against sources ({self.error})._"
        extra = f", {len(self.partial)} only partly supported" if self.partial else ""
        return f"_Verified: {self.checked} claims checked against the stored sources{extra}._"


def gather_sources(files: list[str]) -> list[dict]:
    out = []
    for f in files[:MAX_SOURCES]:
        md = Path(f)
        if not md.exists():
            continue
        meta = read_front_matter(md)
        body = md.read_text().split("\n---\n", 1)[-1]
        facts = "; ".join(f"{k}: {', '.join(map(str, v)) if isinstance(v, list) else v}"
                          for k in META_FIELDS if (v := meta.get(k)) not in (None, "", []))
        text = (f"[catalogue record] {facts}\n" if facts else "") + body
        if meta.get("file", "").lower().endswith(".pdf") and (md.parent / meta["file"]).exists():
            text += "\n" + pdf_text(md.parent / meta["file"], pages=3)
        out.append({"id": f"S{len(out) + 1}", "title": meta.get("title", md.stem),
                    "ref": meta.get("doi") or meta.get("arxiv") or meta.get("source_url") or "",
                    "text": re.sub(r"\s+", " ", text).strip()[:SOURCE_CHARS]})
    return out


def _parse_json(text: str) -> Any:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0) if m else text)


CLAIM_OBJ = re.compile(r'\{\s*"claim"\s*:.*?"verdict"\s*:\s*"[a-z]+".*?\}', re.S)


def _parse_claims(text: str) -> list[dict]:
    """The claims list; if the JSON is cut off (length limit) or malformed, keep every complete claim object."""
    try:
        return list(_parse_json(text).get("claims") or [])
    except (ValueError, AttributeError):
        pass
    out = []
    for m in CLAIM_OBJ.finditer(text or ""):
        try:
            out.append(json.loads(m.group(0)))
        except ValueError:
            continue
    if not out:
        raise ValueError("no claims could be read from the checker's answer")
    return out


def verify_report(llm, report: str, files: list[str]) -> Verification:
    sources = gather_sources(files)
    if not sources or not report.strip():
        return Verification(error="no stored sources to check against")
    src_text = "\n\n".join(f"[{s['id']}] {s['title']} ({s['ref']})\n{s['text']}" for s in sources)
    try:
        r = llm.chat("extractor", [{"role": "user", "content": PROMPT % (report[:6000], src_text)}],
                     max_tokens=8192, temperature=0.0, think=False)      # 5x faster, same verdicts
        claims = _parse_claims(r.text)
    except Exception as e:  # noqa: BLE001 - verification must never block finishing
        return Verification(error=f"{type(e).__name__}")
    v = Verification(checked=len(claims))
    for c in claims:
        verdict = str(c.get("verdict", "")).lower()
        if verdict == "unsupported":
            v.unsupported.append(c)
        elif verdict == "partial":
            v.partial.append(c)
    return v


# -- RG2: corroboration (rigor standard / thorough) ------------------------------------------------------------
# Two steps, so claims stay the same across batches of sources: (1) list the report's claims once; (2) judge
# that fixed, numbered list against each batch of at most MAX_SOURCES sources, recording EVERY source that
# supports, partly supports or contradicts each claim. Independence and confidence are then computed in code.

EXTRACT_PROMPT = """List the factual claims in this research REPORT about content: findings, numbers, conclusions,
what a source says. Skip: checklist lines; what the agent did or saved; bibliographic details (titles, authors,
years, venues, DOIs, arXiv ids, citation counts, licences); the findings table if there is one. At most 20 claims;
merge claims that say the same thing; keep each claim short and self-contained.

Reply with compact JSON only, no other text: {"claims": ["...", "..."]}
If the report makes no such claims, reply {"claims": []}.

REPORT:
<<<
%s
>>>"""

JUDGE_PROMPT = """You check numbered CLAIMS against SOURCES. Use ONLY the sources below.
For each claim, list the ids of the sources that:
- "supports": state the claim,
- "partial": state part of it, or something close but not the same,
- "contradicts": state the opposite, or a different value for the same thing.
A source that does not mention the claim is in no list.

Reply with compact JSON only, one claim per line, no other text:
{"judgements": [
{"c": 1, "supports": ["S1"], "partial": [], "contradicts": []}
]}

CLAIMS:
%s

SOURCES:
%s"""

CHUNK = 1200                    # evidence pages are long: the checker sees their most relevant chunks


def _relevant_text(text: str, claims: list[str], budget: int = SOURCE_CHARS) -> str:
    """The parts of a long text that share the most words with the claims, in their original order."""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= budget:
        return text
    want = {w for c in claims for w in re.findall(r"\w{4,}", c.lower())}
    chunks = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)]
    score = [(len(want & set(re.findall(r"\w{4,}", ch.lower()))), -i) for i, ch in enumerate(chunks)]
    keep = sorted(sorted(range(len(chunks)), key=lambda i: score[i], reverse=True)[:max(1, budget // CHUNK)])
    return " … ".join(chunks[i] for i in keep)[:budget]


def gather_all_sources(files: list[str], evidence: list[dict]) -> list[dict]:
    """Library items (as gather_sources, without its cap) and evidence pages, with a url for independence."""
    from .evidence import text_of
    out = []
    for f in files:
        md = Path(f)
        if not md.exists():
            continue
        meta = read_front_matter(md)
        body = md.read_text().split("\n---\n", 1)[-1]
        facts = "; ".join(f"{k}: {', '.join(map(str, v)) if isinstance(v, list) else v}"
                          for k in META_FIELDS if (v := meta.get(k)) not in (None, "", []))
        text = (f"[catalogue record] {facts}\n" if facts else "") + body
        if meta.get("file", "").lower().endswith(".pdf") and (md.parent / meta["file"]).exists():
            text += "\n" + pdf_text(md.parent / meta["file"], pages=3)
        url = meta.get("source_url") or (f"https://doi.org/{meta['doi']}" if meta.get("doi") else "") \
            or (f"https://arxiv.org/abs/{meta['arxiv']}" if meta.get("arxiv") else "")
        out.append({"title": meta.get("title", md.stem), "url": url, "text": text, "kind": "library"})
    for e in evidence:
        text = text_of(e)
        if text:
            out.append({"title": e.get("title") or e.get("url", ""), "url": e.get("url", ""), "text": text,
                        "kind": "evidence"})
    for i, s in enumerate(out, 1):
        s["id"] = f"S{i}"
    return out


@dataclass
class Corroboration:
    claims: list[dict] = field(default_factory=list)     # claim, supports, partial, contradicts, independent, confidence
    sources: list[dict] = field(default_factory=list)    # id, title, url, kind
    unchecked: int = 0                                    # batches of sources the checker did not answer
    error: str | None = None

    @property
    def unsupported(self) -> list[dict]:
        return [c for c in self.claims if c["verdict"] == "unsupported"]

    @property
    def partial(self) -> list[dict]:
        return [c for c in self.claims if c["verdict"] == "partial"]

    @property
    def checked(self) -> int:
        return len(self.claims)

    @property
    def ok(self) -> bool:
        return self.error is None and not self.unsupported

    def note(self) -> str:
        if self.error:
            return f"_Not verified against sources ({self.error})._"
        n = len(self.claims)
        high = sum(c["confidence"] == "high" for c in self.claims)
        gap = f" ({self.unchecked} batch(es) of sources could not be checked)" if self.unchecked else ""
        return (f"_Verified: {n} claims checked against {len(self.sources)} sources{gap}; "
                f"{high} corroborated by two or more independent sources._")


def confidence(independent: int, partial_independent: int, conflict: bool) -> str:
    """RG5, computed from the record: high = two or more independent supporting sources and no conflict;
    medium = one, or partial support from two or more; low = partial support only, or a conflict;
    unsupported = none."""
    if independent >= 2 and not conflict:
        return "high"
    if independent >= 1 and conflict:
        return "low"
    if independent >= 1 or partial_independent >= 2:
        return "medium"
    if partial_independent >= 1:
        return "low"
    return "unsupported"


def _ids(value: Any, known: set[str]) -> list[str]:
    return [s for s in (value or []) if isinstance(s, str) and s in known]


def corroborate_report(llm, report: str, files: list[str], evidence: list[dict]) -> Corroboration:
    """RG2: every supporting source per claim, over the library items and the evidence pages, in batches."""
    from .independence import independent_groups
    sources = gather_all_sources(files, evidence)
    if not sources or not report.strip():
        return Corroboration(error="no stored sources to check against")
    try:
        r = llm.chat("extractor", [{"role": "user", "content": EXTRACT_PROMPT % report[:6000]}],
                     max_tokens=4096, temperature=0.0, think=False)
        claims = [str(c).strip() for c in (_parse_json(r.text).get("claims") or []) if str(c).strip()][:20]
    except Exception as e:  # noqa: BLE001 - verification must never block finishing
        return Corroboration(error=f"{type(e).__name__} (listing claims)")
    out = Corroboration(sources=[{k: s[k] for k in ("id", "title", "url", "kind")} for s in sources])
    if not claims:
        return out
    marks = [{"supports": set(), "partial": set(), "contradicts": set()} for _ in claims]
    numbered = "\n".join(f"{i}. {c}" for i, c in enumerate(claims, 1))
    failed = 0
    for start in range(0, len(sources), MAX_SOURCES):
        batch = sources[start:start + MAX_SOURCES]
        known = {s["id"] for s in batch}
        src_text = "\n\n".join(f"[{s['id']}] {s['title']} ({s['url']})\n{_relevant_text(s['text'], claims)}"
                               for s in batch)
        try:
            r = llm.chat("extractor", [{"role": "user", "content": JUDGE_PROMPT % (numbered, src_text)}],
                         max_tokens=8192, temperature=0.0, think=False)
            judgements = _parse_json(r.text).get("judgements") or []
        except Exception:  # noqa: BLE001 - one failed batch: the claims keep what other batches found
            failed += 1
            continue
        for j in judgements:
            try:
                i = int(j.get("c")) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= i < len(claims):
                for k in marks[i]:
                    marks[i][k] |= set(_ids(j.get(k), known))
    if failed and failed == -(-len(sources) // MAX_SOURCES):
        return Corroboration(error="the checker answered no batch", sources=out.sources)
    # Group every source once (same site, or near-copies, transitively); a claim's independent sources are the
    # distinct groups among its supporters. Grouping over all sources can only merge more: undercounting is safe.
    groups = independent_groups([{"url": s["url"], "text": s["text"]} for s in sources])
    group_of = {sources[i]["id"]: g for g, members in enumerate(groups) for i in members}

    def independent(ids: set[str]) -> int:
        return len({group_of[i] for i in ids})

    for claim, m in zip(claims, marks):
        ind = independent(m["supports"])
        part = independent(m["supports"] | m["partial"])
        conf = confidence(ind, part, bool(m["contradicts"]))
        verdict = "supported" if m["supports"] else ("partial" if m["partial"] else "unsupported")
        out.claims.append({"claim": claim, "verdict": verdict, "supports": sorted(m["supports"], key=lambda s: int(s[1:])),
                           "partial": sorted(m["partial"], key=lambda s: int(s[1:])),
                           "contradicts": sorted(m["contradicts"], key=lambda s: int(s[1:])),
                           "independent": ind, "confidence": conf})
    out.unchecked = failed
    return out


def findings_table(c: Corroboration) -> str:
    """RG5: the findings with confidence computed from the record, then what rests on one source or conflicts."""
    rows = [c_ for c_ in c.claims if c_["verdict"] != "unsupported"]
    if not rows:
        return ""
    by_id = {s["id"]: s for s in c.sources}

    def refs(ids: list[str]) -> str:
        return ", ".join(f"{i} {_short(by_id[i])}" for i in ids if i in by_id) or "—"

    lines = ["## Findings and confidence", "",
             "| Finding | Confidence | Supported by | Contradicted by |", "| --- | --- | --- | --- |"]
    for r in rows:
        claim = r["claim"].replace("|", "/")[:200]
        lines.append(f"| {claim} | {r['confidence']} ({r['independent']} independent) | "
                     f"{refs(r['supports'] or r['partial'])} | {refs(r['contradicts'])} |")
    single = [r for r in rows if r["independent"] <= 1 and not r["contradicts"]]
    conflicts = [r for r in rows if r["contradicts"]]
    if single:
        lines += ["", f"_{len(single)} finding(s) rest on a single source: treat them as unconfirmed._"]
    if conflicts:
        lines += ["", f"_{len(conflicts)} finding(s) are contradicted by at least one source: see the last column._"]
    return "\n".join(lines)


def _short(s: dict) -> str:
    from .independence import registrable_domain
    return registrable_domain(s.get("url", "")) or (s.get("title") or "")[:40]
