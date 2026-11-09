"""Vision on real UI screenshots: reading text, comparing values, and click grounding.

Fixture pages are rendered locally with Playwright at 1280x800, so element
bounding boxes are known exactly and grounding answers can be scored.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from ..core import CaseResult, Meter, guarded, norm, parse_json_loose, strip_thinking

NAME = "vision"
W, H = 1280, 800
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
CACHE = Path(__file__).resolve().parent.parent / ".cache"


@dataclass
class Case:
    name: str
    page: str            # fixture file stem
    question: str
    kind: str            # "text" | "point"
    expected: str = ""   # substring for text answers
    target: str = ""     # element id for point answers


CASES = [
    Case("read_primary_button", "form", "What text is on the blue primary button? Reply with the button text only.",
         "text", expected="submit application"),
    Case("read_field_value", "form", "What is the value in the Email field? Reply with the value only.",
         "text", expected="n.georgiou@example.org"),
    Case("read_error_message", "form", "Is there a validation error on the page? Quote it exactly.",
         "text", expected="must start with +30"),
    Case("compare_table_values", "table",
         "Which dataset has the highest monthly price? Reply with the dataset name only.",
         "text", expected="flowmap gas nominations"),
    Case("ground_next_button", "table", "Where should I click to go to the next page of results?",
         "point", target="next"),
    Case("ground_email_input", "form", "Where should I click to edit the Email field?",
         "point", target="email"),
    Case("ground_reject_cookies", "cookies",
         "Where should I click to decline non-essential cookies?", "point", target="reject"),
]

POINT_INSTRUCTION = (
    f"The screenshot is {W}x{H} pixels. Reply with only a JSON object "
    '{"x": <int>, "y": <int>} giving the pixel coordinates of the centre of the element to click.'
)


def render_fixtures() -> dict[str, dict]:
    """Render each fixture to PNG once (cached by content hash) and record element boxes."""
    CACHE.mkdir(exist_ok=True)
    out: dict[str, dict] = {}
    todo = []
    for html in sorted(FIXTURES.glob("*.html")):
        digest = hashlib.sha1(html.read_bytes()).hexdigest()[:12]
        png, meta = CACHE / f"{html.stem}-{digest}.png", CACHE / f"{html.stem}-{digest}.json"
        if png.exists() and meta.exists():
            out[html.stem] = {"png": png.read_bytes(), "boxes": json.loads(meta.read_text())}
        else:
            todo.append((html, png, meta))
    if todo:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": W, "height": H}, device_scale_factor=1)
            for html, png, meta in todo:
                page.goto(html.resolve().as_uri())
                boxes = page.evaluate(
                    "() => Object.fromEntries([...document.querySelectorAll('[id]')].map(e => {"
                    "const r = e.getBoundingClientRect(); return [e.id, [r.x, r.y, r.width, r.height]]; }))")
                data = page.screenshot(full_page=False)
                png.write_bytes(data)
                meta.write_text(json.dumps(boxes))
                out[html.stem] = {"png": data, "boxes": boxes}
            browser.close()
    return out


def _inside(x: float, y: float, box: list[float], tol: float = 6) -> bool:
    bx, by, bw, bh = box
    return bx - tol <= x <= bx + bw + tol and by - tol <= y <= by + bh + tol


def _run_case(llm, case: Case, fixtures: dict):
    def fn(meter: Meter) -> CaseResult:
        fx = fixtures[case.page]
        img = base64.b64encode(fx["png"]).decode()
        prompt = case.question + ("\n\n" + POINT_INSTRUCTION if case.kind == "point" else "")
        messages = [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img}"}},
            {"type": "text", "text": prompt},
        ]}]
        r = llm.chat("vision", messages, max_tokens=300)
        meter.add(r)
        answer = strip_thinking(r.text)

        if case.kind == "text":
            ok = case.expected in norm(answer)
            return CaseResult(suite=NAME, case=case.name, passed=ok, score=float(ok),
                              detail="correct" if ok else f"expected '{case.expected}', got {answer[:100]!r}",
                              extra={"answer": answer[:300]})

        pt = parse_json_loose(answer)
        x, y = float(pt["x"]), float(pt["y"])
        box = fx["boxes"][case.target]
        as_pixels = _inside(x, y, box)
        # Many Qwen-VL builds answer on a 0-1000 grid instead of pixels.
        as_grid = _inside(x * W / 1000, y * H / 1000, box)
        ok = as_pixels or as_grid
        convention = "pixels" if as_pixels else ("0-1000 grid" if as_grid else "miss")
        cx, cy = box[0] + box[2] / 2, box[1] + box[3] / 2
        return CaseResult(
            suite=NAME, case=case.name, passed=ok, score=float(ok),
            detail=(f"hit ({convention})" if ok else
                    f"missed: answered ({x:.0f},{y:.0f}), target centre ({cx:.0f},{cy:.0f})"),
            extra={"answer": answer[:200], "convention": convention, "target_box": box},
        )
    return fn


def run(llm, repeats: int = 1) -> list[CaseResult]:
    fixtures = render_fixtures()
    out = []
    for i in range(repeats):
        for case in CASES:
            name = case.name if repeats == 1 else f"{case.name}#{i + 1}"
            res = guarded(NAME, name, _run_case(llm, case, fixtures))
            res.case = name
            out.append(res)
    return out
