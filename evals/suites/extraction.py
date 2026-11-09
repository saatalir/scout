"""Structured extraction: page text in, JSON out, scored field by field."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core import CaseResult, Meter, guarded, norm, parse_json_loose, strip_thinking

NAME = "extraction"

PAPER_PAGE = """\
arXiv:2503.04127 [cs.CL]  Submitted on 6 Mar 2025

# Sparse Expert Caching for Consumer-GPU Inference of Mixture-of-Experts Models

Authors: Eleni Markou, Jonas Weber, Priya Raman

Abstract: Mixture-of-Experts language models activate a small subset of parameters per
token, yet their total size exceeds consumer GPU memory. We propose a tiered expert
cache spanning VRAM, system RAM and NVMe ...

Comments: 14 pages, 6 figures
DOI: https://doi.org/10.48550/arXiv.2503.04127
Access: [View PDF](https://arxiv.org/pdf/2503.04127)  [HTML (experimental)](https://arxiv.org/html/2503.04127v1)
"""

JOB_PAGE = """\
Careers > Engineering > Athens

Data Engineer (Grid Analytics)
Helios Energy S.A. · Athens, Greece · Hybrid (3 days on site)

We are looking for a Data Engineer to build pipelines for transmission-network telemetry.
Gross annual salary: EUR 38,000 – 46,000, depending on experience.
Applications close on 15 December 2026.

Requirements: Python, SQL, Spark; experience with time-series data is a plus.

Apply at https://careers.helios-energy.example/jobs/4471/apply
Questions? hr@helios-energy.example
"""

DATASET_PAGE_GREEK = """\
Ανοιχτά Δεδομένα > Ενέργεια

Ημερήσια κατανάλωση φυσικού αερίου ανά σημείο εξόδου (2025)
Φορέας: Διαχειριστής Δικτύου Φυσικού Αερίου
Άδεια χρήσης: CC BY 4.0
Μορφή αρχείου: CSV · Μέγεθος: 12,4 MB
Τελευταία ενημέρωση: 3 Φεβρουαρίου 2026
Λήψη: https://data.example.gr/datasets/gas-exit-points-2025.csv
"""


@dataclass
class Case:
    name: str
    page: str
    instruction: str
    expected: dict[str, Any]


CASES = [
    Case("paper_metadata", PAPER_PAGE,
         "Return JSON with keys: title (string), authors (list of strings), year (integer), "
         "doi (string, bare DOI without https://doi.org/), pdf_url (string).",
         {"title": "Sparse Expert Caching for Consumer-GPU Inference of Mixture-of-Experts Models",
          "authors": ["Eleni Markou", "Jonas Weber", "Priya Raman"],
          "year": 2025, "doi": "10.48550/arXiv.2503.04127",
          "pdf_url": "https://arxiv.org/pdf/2503.04127"}),
    Case("job_posting", JOB_PAGE,
         "Return JSON with keys: title, company, city, deadline (YYYY-MM-DD), salary_min (integer), "
         "salary_max (integer), currency (ISO code), apply_url.",
         {"title": "Data Engineer (Grid Analytics)", "company": "Helios Energy S.A.", "city": "Athens",
          "deadline": "2026-12-15", "salary_min": 38000, "salary_max": 46000, "currency": "EUR",
          "apply_url": "https://careers.helios-energy.example/jobs/4471/apply"}),
    Case("greek_dataset_page", DATASET_PAGE_GREEK,
         "Return JSON in English with keys: license (string), format (string), size_mb (number), "
         "last_updated (YYYY-MM-DD), download_url (string).",
         {"license": "CC BY 4.0", "format": "CSV", "size_mb": 12.4,
          "last_updated": "2026-02-03",
          "download_url": "https://data.example.gr/datasets/gas-exit-points-2025.csv"}),
]


def _match(expected: Any, got: Any) -> bool:
    if isinstance(expected, list):
        return isinstance(got, list) and [norm(x) for x in got] == [norm(x) for x in expected]
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        try:
            return abs(float(got) - float(expected)) < 1e-6
        except (TypeError, ValueError):
            return False
    return norm(got) == norm(expected)


def _run_case(llm, case: Case):
    def fn(meter: Meter) -> CaseResult:
        messages = [
            {"role": "system", "content": "You extract structured data from web pages. "
                                          "Reply with a single JSON object and nothing else."},
            {"role": "user", "content": f"{case.instruction}\n\nPAGE:\n{case.page}"},
        ]
        r = llm.chat("extractor", messages, max_tokens=500)
        meter.add(r)
        data = parse_json_loose(strip_thinking(r.text))
        if not isinstance(data, dict):
            raise ValueError("reply is not a JSON object")
        field_ok = {k: _match(v, data.get(k)) for k, v in case.expected.items()}
        wrong = {k: data.get(k) for k, ok in field_ok.items() if not ok}
        score = sum(field_ok.values()) / len(field_ok)
        return CaseResult(
            suite=NAME, case=case.name, passed=not wrong, score=round(score, 3),
            detail="all fields correct" if not wrong else f"wrong fields: {wrong}",
            extra={"got": data},
        )
    return fn


def run(llm, repeats: int = 1) -> list[CaseResult]:
    out = []
    for i in range(repeats):
        for case in CASES:
            name = case.name if repeats == 1 else f"{case.name}#{i + 1}"
            res = guarded(NAME, name, _run_case(llm, case))
            res.case = name
            out.append(res)
    return out
