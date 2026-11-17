"""Approval policy: who approves irreversible actions (submit, send, apply, pay, delete…).

    SCOUT_APPROVALS=ask    the human approves each one (default)
    SCOUT_APPROVALS=auto   no human in the loop: Scout approves its own actions and records each auto-approval
    SCOUT_APPROVALS=deny   unattended dry run: nothing irreversible happens (forms are filled, never submitted)

Limits for auto mode:
    SCOUT_AUTO_APPROVE_DOMAINS="example.org,forms.example.com"   only these sites (and their subdomains);
                                                                  empty = any site
    SCOUT_AUTO_APPROVE_PAYMENTS=yes                              payment-like actions (pay, buy, checkout,
                                                                  transfer, donate…) are denied in auto mode
                                                                  unless this is set

Hard limits that no mode lifts: Scout never types passwords and never gets past CAPTCHAs (AGENTS.md).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlparse

from dotenv import load_dotenv

from .config import ROOT

MODES = ("ask", "auto", "deny")
PAYMENT = re.compile(r"\b(pay|payment|purchase|buy|checkout|check out|place order|order now|transfer|donate|"
                     r"billing|card|subscribe and pay)\b", re.I)


@dataclass
class Decision:
    approved: bool
    how: str                     # "human", "auto", or why it was denied


@dataclass
class ApprovalPolicy:
    mode: str = "ask"
    domains: list[str] = field(default_factory=list)
    allow_payments: bool = False

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"SCOUT_APPROVALS must be one of {MODES}, got {self.mode!r}")

    @classmethod
    def from_env(cls, mode: str | None = None) -> "ApprovalPolicy":
        load_dotenv(ROOT / ".env", override=False)
        return cls(mode=(mode or os.getenv("SCOUT_APPROVALS") or "ask").strip().lower(),
                   domains=[d.strip().lower() for d in os.getenv("SCOUT_AUTO_APPROVE_DOMAINS", "").split(",") if d.strip()],
                   allow_payments=os.getenv("SCOUT_AUTO_APPROVE_PAYMENTS", "").strip().lower() in ("yes", "true", "1"))

    def _domain_ok(self, url: str | None) -> bool:
        if not self.domains:
            return True
        host = (urlparse(url).hostname or "").lower() if url else ""
        return bool(host) and any(host == d or host.endswith("." + d) for d in self.domains)

    def decide(self, summary: str, url: str | None, ask_human: Callable[[str], bool]) -> Decision:
        if self.mode == "ask":
            return Decision(bool(ask_human(summary)), "human")
        if self.mode == "deny":
            return Decision(False, "denied: SCOUT_APPROVALS=deny (dry run)")
        if PAYMENT.search(summary) and not self.allow_payments:
            return Decision(False, "denied: payment-like action; auto mode needs SCOUT_AUTO_APPROVE_PAYMENTS=yes")
        if not self._domain_ok(url):
            return Decision(False, f"denied: {urlparse(url).hostname if url else 'unknown site'} is not in "
                                   "SCOUT_AUTO_APPROVE_DOMAINS")
        return Decision(True, "auto")
