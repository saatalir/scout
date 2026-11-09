"""Speed: time to first token, decode rate and prefill rate over the tailnet.

Informational: these cases pass whenever they complete. The numbers set
step budgets for the agent loop (a 30-step task at 20 s/step is 10 minutes).
"""

from __future__ import annotations

import time

from ..core import CaseResult, Meter, guarded

NAME = "speed"

FILLER = ("Gas demand in southern Europe rose year on year while storage stayed above the "
          "five-year average, supported by steady LNG arrivals and mild weather. ") * 1


def _long_context(target_tokens: int) -> str:
    # ~4 characters per token for English prose.
    chunk = FILLER
    return (chunk * (target_tokens * 4 // len(chunk) + 1))[: target_tokens * 4]


CASES = [
    ("short_prompt_decode", "Write a 150-word description of how a gas transmission network works.", 400),
    ("long_prompt_8k_prefill",
     _long_context(8000) + "\n\nIn one sentence: what is this text about?", 60),
    ("long_prompt_24k_prefill",
     _long_context(24000) + "\n\nIn one sentence: what is this text about?", 60),
]


def _run_case(llm, prompt: str, max_tokens: int, name: str):
    def fn(meter: Meter) -> CaseResult:
        model = llm.model_for("planner")
        kwargs = dict(model=model, messages=[{"role": "user", "content": prompt}],
                      max_tokens=max_tokens, temperature=0.0, stream=True)
        t0 = time.perf_counter()
        try:
            stream = llm.client.chat.completions.create(stream_options={"include_usage": True}, **kwargs)
        except Exception:  # noqa: BLE001 - server may not accept stream_options
            stream = llm.client.chat.completions.create(**kwargs)
        ttft, chars, usage = None, 0, None
        for chunk in stream:
            if getattr(chunk, "usage", None):
                usage = chunk.usage
            if chunk.choices:
                delta = chunk.choices[0].delta
                piece = (delta.content or "") + (getattr(delta, "reasoning_content", None) or "")
                if piece and ttft is None:
                    ttft = time.perf_counter() - t0
                chars += len(piece)
        total = time.perf_counter() - t0
        ttft = ttft if ttft is not None else total

        estimated = usage is None
        out_tokens = usage.completion_tokens if usage else max(1, chars // 4)
        in_tokens = usage.prompt_tokens if usage else len(prompt) // 4
        decode_time = max(1e-6, total - ttft)
        metrics = {
            "ttft_s": round(ttft, 2),
            "total_s": round(total, 2),
            "prompt_tokens": in_tokens,
            "completion_tokens": out_tokens,
            "decode_tok_per_s": round(out_tokens / decode_time, 1) if out_tokens > 1 else None,
            "prefill_tok_per_s": round(in_tokens / ttft, 1) if ttft > 0 else None,
            "token_counts_estimated": estimated,
        }
        meter.latency_s, meter.steps = total, 1
        meter.prompt_tokens, meter.completion_tokens = in_tokens, out_tokens
        detail = f"TTFT {metrics['ttft_s']}s, decode {metrics['decode_tok_per_s']} tok/s, " \
                 f"prefill {metrics['prefill_tok_per_s']} tok/s" + (" (estimated counts)" if estimated else "")
        return CaseResult(suite=NAME, case=name, passed=True, score=1.0, detail=detail, extra=metrics)
    return fn


def run(llm, repeats: int = 1) -> list[CaseResult]:
    out = []
    for i in range(repeats):
        for name, prompt, max_tokens in CASES:
            label = name if repeats == 1 else f"{name}#{i + 1}"
            res = guarded(NAME, label, _run_case(llm, prompt, max_tokens, label))
            res.case = label
            out.append(res)
    return out
