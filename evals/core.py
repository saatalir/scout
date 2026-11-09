"""Shared types and helpers for the eval suites."""

from __future__ import annotations

import json
import re
import time
import traceback
from dataclasses import asdict, dataclass, field
from typing import Any, Callable


@dataclass
class CaseResult:
    suite: str
    case: str
    passed: bool
    score: float = 0.0          # 0..1, partial credit where it makes sense
    latency_s: float = 0.0      # total model time for the case
    steps: int = 0              # model calls made
    prompt_tokens: int = 0
    completion_tokens: int = 0
    detail: str = ""
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


class Meter:
    """Accumulates latency and token counts across the model calls of one case."""

    def __init__(self) -> None:
        self.latency_s = 0.0
        self.steps = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.truncated = 0      # calls cut off by max_tokens (finish_reason == "length")

    def add(self, r) -> None:
        self.latency_s += r.latency_s
        self.steps += 1
        self.prompt_tokens += r.prompt_tokens or 0
        self.completion_tokens += r.completion_tokens or 0
        try:
            if r.raw.choices[0].finish_reason == "length":
                self.truncated += 1
        except (AttributeError, IndexError, TypeError):
            pass

    def fill(self, res: CaseResult) -> CaseResult:
        res.latency_s = round(self.latency_s, 3)
        res.steps = self.steps
        res.prompt_tokens = self.prompt_tokens
        res.completion_tokens = self.completion_tokens
        if self.truncated:
            res.extra["truncated_calls"] = self.truncated
        return res


def guarded(suite: str, case: str, fn: Callable[[Meter], CaseResult]) -> CaseResult:
    """Run one case; turn any exception into a failed result instead of aborting the run."""
    meter = Meter()
    t0 = time.perf_counter()
    try:
        return meter.fill(fn(meter))
    except Exception as e:  # noqa: BLE001
        res = CaseResult(suite=suite, case=case, passed=False,
                         error=f"{type(e).__name__}: {e}",
                         detail=traceback.format_exc(limit=3)[-600:])
        meter.fill(res)
        res.latency_s = res.latency_s or round(time.perf_counter() - t0, 3)
        return res


def tool(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": required if required is not None else list(properties)},
    }}


_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
          "array": list, "object": dict}


def validate_args(schema: dict, args: dict) -> list[str]:
    """Light JSON-schema check: required keys, primitive types, enums, unknown keys."""
    problems: list[str] = []
    props = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in args:
            problems.append(f"missing required '{key}'")
    for key, val in args.items():
        spec = props.get(key)
        if spec is None:
            problems.append(f"unknown argument '{key}'")
            continue
        expected = _TYPES.get(spec.get("type", ""))
        if expected and not (isinstance(val, expected) and not (spec.get("type") in ("integer", "number") and isinstance(val, bool))):
            problems.append(f"'{key}' should be {spec['type']}, got {type(val).__name__}")
        if "enum" in spec and val not in spec["enum"]:
            problems.append(f"'{key}'={val!r} not in {spec['enum']}")
    return problems


def parse_json_loose(text: str) -> Any:
    """Parse JSON from a model reply, tolerating code fences and surrounding prose."""
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for open_, close in (("{", "}"), ("[", "]")):
        start, end = text.find(open_), text.rfind(close)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError(f"no JSON found in reply: {text[:120]!r}")


def norm(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s)).strip().lower()


def strip_thinking(text: str) -> str:
    """Drop <think>…</think> blocks that some Qwen builds emit in content."""
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
