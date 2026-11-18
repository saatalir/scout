"""Shared test setup: tests never depend on the developer's own .env choices for approvals or Tor."""

import pytest


@pytest.fixture(autouse=True)
def _neutral_policy_env(monkeypatch):
    monkeypatch.setenv("SCOUT_APPROVALS", "ask")
    monkeypatch.setenv("SCOUT_AUTO_APPROVE_DOMAINS", "")
    monkeypatch.setenv("SCOUT_AUTO_APPROVE_PAYMENTS", "no")
    monkeypatch.delenv("SCOUT_CHAOS_DOWN", raising=False)
    monkeypatch.setenv("SCOUT_CONSENT_WAIT", "0")          # no waiting for late consent banners (tests that need it set it)
