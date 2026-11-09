"""Settings loaded from the environment (and .env)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROLES = ("planner", "browser", "vision", "extractor")

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    strata_base_url: str
    strata_api_key: str
    strata_model: str
    router: str  # "direct" | "litellm"
    litellm_base_url: str
    litellm_api_key: str
    request_timeout: float

    @property
    def base_url(self) -> str:
        return self.litellm_base_url if self.router == "litellm" else self.strata_base_url

    @property
    def api_key(self) -> str:
        return self.litellm_api_key if self.router == "litellm" else self.strata_api_key


def load_settings(env_file: str | os.PathLike | None = None) -> Settings:
    load_dotenv(env_file or ROOT / ".env", override=False)
    router = os.getenv("SCOUT_ROUTER", "direct").strip().lower()
    if router not in ("direct", "litellm"):
        raise ValueError(f"SCOUT_ROUTER must be 'direct' or 'litellm', got {router!r}")
    return Settings(
        strata_base_url=os.getenv("STRATA_BASE_URL", "http://strata-box:8080/v1").rstrip("/"),
        strata_api_key=os.getenv("STRATA_API_KEY", "none") or "none",
        strata_model=os.getenv("STRATA_MODEL", "auto"),
        router=router,
        litellm_base_url=os.getenv("LITELLM_BASE_URL", "http://127.0.0.1:4000/v1").rstrip("/"),
        litellm_api_key=os.getenv("LITELLM_API_KEY", "sk-scout-local"),
        request_timeout=float(os.getenv("SCOUT_REQUEST_TIMEOUT", "300")),
    )
