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
