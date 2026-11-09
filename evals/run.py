"""Run the Phase 0 evals and write a gate report.

    python -m evals.run                       # all suites, 1 repeat
    python -m evals.run --repeats 3           # recommended for the gate decision
    python -m evals.run --suites tool_calling,agent_loop
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

from scout.config import ROOT, load_settings
from scout.llm import LLM

from .core import CaseResult
from .suites import SUITES

THRESHOLDS = Path(__file__).resolve().parent / "thresholds.yaml"


def suite_stats(results: list[CaseResult]) -> dict:
    n = len(results)
    return {
        "cases": n,
        "pass_rate": round(sum(r.passed for r in results) / n, 3) if n else 0.0,
        "mean_score": round(statistics.mean(r.score for r in results), 3) if n else 0.0,
        "errors": sum(1 for r in results if r.error),
        "median_latency_s": round(statistics.median(r.latency_s for r in results), 2) if n else 0.0,
        "mean_steps": round(statistics.mean(r.steps for r in results), 1) if n else 0.0,
    }


def suite_verdict(name: str, stats: dict, results: list[CaseResult], th: dict) -> tuple[bool, str]:
    rules = th.get(name, {})
    if name == "speed":
        short = [r for r in results if r.case.startswith("short_prompt") and not r.error]
        if not short:
            return False, "no successful speed run"
        ttft = statistics.median(r.extra["ttft_s"] for r in short)
        dec = [r.extra["decode_tok_per_s"] for r in short if r.extra.get("decode_tok_per_s")]
        dec_m = statistics.median(dec) if dec else 0.0
        ok = ttft <= rules.get("max_ttft_s", 1e9) and dec_m >= rules.get("min_decode_tok_per_s", 0)
        return ok, f"TTFT {ttft:.1f}s (max {rules.get('max_ttft_s')}), decode {dec_m:.0f} tok/s (min {rules.get('min_decode_tok_per_s')})"
    parts, ok = [], True
    for key in ("pass_rate", "mean_score"):
        if key in rules:
            good = stats[key] >= rules[key]
            ok &= good
            parts.append(f"{key} {stats[key]:.0%} (needs {rules[key]:.0%})")
    return ok, ", ".join(parts)


def write_report(out_dir: Path, meta: dict, by_suite: dict, verdicts: dict, th: dict) -> str:
    lines = [f"# Phase 0 eval — {meta['started']}", "",
             f"Model: `{meta['model']}` via {meta['router']} ({meta['endpoint']}). "
             f"Repeats: {meta['repeats']}. Wall time: {meta['wall_s']:.0f}s.", "",
             "## Gate", "", "| Role | Decided by | Result | Recommendation |", "| --- | --- | --- | --- |"]
    for role, suites in th.get("roles", {}).items():
        ran = [s for s in suites if s in verdicts]
        if not ran:
            lines.append(f"| {role} | {', '.join(suites)} | not run | — |")
            continue
        ok = all(verdicts[s][0] for s in ran)
        rec = "Keep on Strata" if ok else "Route to fallback (cloud or second local model)"
        lines.append(f"| {role} | {', '.join(ran)} | {'PASS' if ok else 'FAIL'} | {rec} |")

    lines += ["", "## Suites", "", "| Suite | Cases | Pass rate | Mean score | Median latency | Mean steps | Errors | Verdict |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for name, results in by_suite.items():
        s = suite_stats(results)
        ok, why = verdicts[name]
        lines.append(f"| {name} | {s['cases']} | {s['pass_rate']:.0%} | {s['mean_score']:.2f} | "
                     f"{s['median_latency_s']}s | {s['mean_steps']} | {s['errors']} | "
                     f"{'PASS' if ok else 'FAIL'}: {why} |")

    lines += ["", "## Cases", "", "| Suite | Case | Result | Score | Steps | Latency | Detail |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    for name, results in by_suite.items():
        for r in results:
            detail = (r.error or r.detail).replace("|", "/").replace("\n", " ")[:160]
            lines.append(f"| {name} | {r.case} | {'pass' if r.passed else 'FAIL'} | {r.score:.2f} | "
                         f"{r.steps} | {r.latency_s:.1f}s | {detail} |")
    text = "\n".join(lines) + "\n"
    (out_dir / "summary.md").write_text(text)
    return text


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--suites", default="all", help=f"comma list from: {', '.join(SUITES)}")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--out", default=str(ROOT / "results"))
    args = ap.parse_args(argv)

    names = list(SUITES) if args.suites == "all" else [s.strip() for s in args.suites.split(",")]
    unknown = [n for n in names if n not in SUITES]
    if unknown:
        ap.error(f"unknown suite(s): {unknown}")

    settings = load_settings()
    llm = LLM(settings)
    model = llm.model_for("planner")
    th = yaml.safe_load(THRESHOLDS.read_text())

    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out_dir = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Model {model} via {settings.router} at {settings.base_url}; writing to {out_dir}")

    t0 = time.perf_counter()
    by_suite: dict[str, list[CaseResult]] = {}
    with open(out_dir / "results.jsonl", "w") as fh:
        for name in names:
            print(f"\n== {name}")
            results = []
            for r in SUITES[name].run(llm, args.repeats):
                fh.write(r.to_json() + "\n")
                fh.flush()
                print(f"  {'pass' if r.passed else 'FAIL'}  {r.case:<32} {r.latency_s:6.1f}s  "
                      f"{(r.error or r.detail)[:90]}")
                results.append(r)
            by_suite[name] = results

    verdicts = {n: suite_verdict(n, suite_stats(rs), rs, th) for n, rs in by_suite.items()}
    meta = {"started": started, "model": model, "router": settings.router, "endpoint": settings.base_url,
            "repeats": args.repeats, "wall_s": time.perf_counter() - t0,
            "host": platform.node(), "python": sys.version.split()[0]}
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    report = write_report(out_dir, meta, by_suite, verdicts, th)
    print("\n" + report.split("## Cases")[0].strip())
    print(f"\nFull report: {out_dir / 'summary.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
