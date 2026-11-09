"""Role-based access to the model layer.

Agents never name a model. They ask for a role (planner, browser, vision,
extractor). In "direct" mode every role resolves to the model Strata serves;
in "litellm" mode the role name itself is sent and the proxy maps it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI

from .config import ROLES, Settings, load_settings


@dataclass
class ChatResult:
    message: Any                  # openai ChatCompletionMessage
    latency_s: float
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    raw: Any = field(default=None, repr=False)

    @property
    def text(self) -> str:
        return self.message.content or ""

    @property
    def tool_calls(self) -> list:
        return list(self.message.tool_calls or [])


class LLM:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or load_settings()
        self.client = OpenAI(
            base_url=self.settings.base_url,
            api_key=self.settings.api_key,
            timeout=self.settings.request_timeout,
            max_retries=1,
        )
        self._resolved_model: str | None = None

    # -- model resolution -------------------------------------------------
    def list_models(self) -> list[str]:
        return [m.id for m in self.client.models.list().data]

    def model_for(self, role: str) -> str:
        if role not in ROLES:
            raise ValueError(f"Unknown role {role!r}; expected one of {ROLES}")
        if self.settings.router == "litellm":
            return role
        if self._resolved_model is None:
            configured = self.settings.strata_model
            if configured and configured != "auto":
                self._resolved_model = configured
            else:
                models = self.list_models()
                if not models:
                    raise RuntimeError(f"No models listed at {self.settings.base_url}/models")
                self._resolved_model = models[0]
        return self._resolved_model

    # -- calls ------------------------------------------------------------
    def chat(
        self,
        role: str,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        **extra: Any,
    ) -> ChatResult:
        kwargs: dict[str, Any] = dict(
            model=self.model_for(role),
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            **extra,
        )
        if tools:
            kwargs["tools"] = tools
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
        t0 = time.perf_counter()
        resp = self.client.chat.completions.create(**kwargs)
        latency = time.perf_counter() - t0
        usage = getattr(resp, "usage", None)
        return ChatResult(
            message=resp.choices[0].message,
            latency_s=latency,
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
            raw=resp,
        )
