"""Eval suites. Each module exposes NAME and run(llm, repeats) -> list[CaseResult]."""

from . import agent_loop, extraction, speed, tool_calling, vision

SUITES = {m.NAME: m for m in (tool_calling, extraction, agent_loop, vision, speed)}
