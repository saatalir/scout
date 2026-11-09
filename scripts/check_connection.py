"""Check that the Mac can reach Strata (directly or through LiteLLM).

Steps: list models, one plain chat, one tool call, one image request.
Each step reports OK / FAIL with the reason, so a broken setup points
straight at the failing layer (network, server, tool calling, vision).
"""

from __future__ import annotations

import base64
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from scout.config import load_settings  # noqa: E402
from scout.llm import LLM  # noqa: E402

def solid_png(rgb=(220, 30, 30), size=64) -> str:
    """A solid-colour PNG, built in memory, base64-encoded."""
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    row = b"\x00" + bytes(rgb) * size
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(row * size))
           + chunk(b"IEND", b""))
    return base64.b64encode(png).decode()


TINY_PNG = solid_png()


def step(name: str, fn) -> bool:
    t0 = time.perf_counter()
    try:
        detail = fn()
        print(f"  OK    {name:<14} {time.perf_counter() - t0:6.1f}s  {detail}")
        return True
    except Exception as e:  # noqa: BLE001 - report everything
        msg = str(e).splitlines()[0][:160] if str(e) else type(e).__name__
        print(f"  FAIL  {name:<14} {time.perf_counter() - t0:6.1f}s  {type(e).__name__}: {msg}")
        return False


def main() -> int:
    s = load_settings()
    llm = LLM(s)
    print(f"Router: {s.router}   Endpoint: {s.base_url}\n")

    def reach():
        root = s.base_url.rsplit("/v1", 1)[0]
        r = httpx.get(root, timeout=10)
        return f"HTTP {r.status_code} from {root}"

    def models():
        ids = llm.list_models()
        return f"{len(ids)} model(s): {', '.join(ids[:3])}"

    def answered(r) -> str:
        """Fail on an empty answer: a thinking model can spend the whole budget reasoning."""
        if not r.text.strip():
            finish = r.raw.choices[0].finish_reason if r.raw else "?"
            raise RuntimeError(f"empty answer (finish_reason={finish}, {r.completion_tokens or '?'} tokens)")
        return r.text.strip()[:40]

    def chat():
        r = llm.chat("planner", [{"role": "user", "content": "Reply with the single word: ready"}], max_tokens=512)
        return f"{answered(r)!r}  ({r.completion_tokens or '?'} tokens)"

    def tools():
        tool = {"type": "function", "function": {
            "name": "get_weather", "description": "Get the current weather for a city.",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}
        r = llm.chat("planner", [{"role": "user", "content": "What's the weather in Paris?"}], tools=[tool], max_tokens=200)
        if not r.tool_calls:
            raise RuntimeError(f"no tool call; replied {r.text[:80]!r}")
        c = r.tool_calls[0]
        return f"{c.function.name}({c.function.arguments})"

    def vision():
        msg = [{"role": "user", "content": [
            {"type": "text", "text": "What colour is this image? One word."},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{TINY_PNG}"}}]}]
        r = llm.chat("vision", msg, max_tokens=512)
        return f"{answered(r)!r}"

    results = [step("reachable", reach), step("models", models)]
    if results[-1]:
        results += [step("chat", chat), step("tool calling", tools), step("image input", vision)]
    print()
    if all(results):
        print("All checks passed. Next: python -m evals.run")
        return 0
    print("Some checks failed. Network/models failures: check Tailscale and STRATA_BASE_URL.")
    print("Tool-calling or image failures are findings for the Phase 0 eval, not setup errors.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
