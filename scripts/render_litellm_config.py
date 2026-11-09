"""Render config/litellm.generated.yaml from the template.

Resolves STRATA_MODEL (or asks Strata's /v1/models when it is "auto")
and writes a config LiteLLM can load directly.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dataclasses import replace  # noqa: E402

from scout.config import load_settings  # noqa: E402
from scout.llm import LLM  # noqa: E402


def main() -> int:
    settings = replace(load_settings(), router="direct")  # always ask Strata itself
    model = LLM(settings).model_for("planner")
    template = (ROOT / "config" / "litellm.template.yaml").read_text()
    out = ROOT / "config" / "litellm.generated.yaml"
    out.write_text(template.replace("{{STRATA_MODEL}}", model))
    print(f"Wrote {out.relative_to(ROOT)} with Strata model '{model}'.")
    print("Start the proxy with:  litellm --config config/litellm.generated.yaml --port 4000")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
