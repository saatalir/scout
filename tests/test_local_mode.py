"""Local mode: every browser operation runs on this machine instead of the VM.

Offline: no browser is launched. Local mode is only a different pair of search
profiles, so these tests cover the parsing rules, the shipped config, the Tor
profile choice and the CLI's --where switch. They leave the process environment
exactly as they found it, so the browser tests that run after them are unaffected.
"""

from __future__ import annotations

import os

import pytest

from scout.browser.config import ConfigError, load_browser_config, parse_browser_config
from scout.tools.search import resolve_tor


def _raw(**search) -> dict:
    """A small config with both roles on both hosts; `search` overrides the section."""
    return {
        "hosts": {"vm": {"kind": "ssh", "address": "vm", "user": "scout"}, "local": {"kind": "local"}},
        "profiles": {
            "research": {"host": "vm", "port": 9301},
            "research-tor": {"host": "vm", "port": 9302, "proxy": "socks5://127.0.0.1:9050"},
            "local-research": {"host": "local", "port": 9311},
            "local-research-tor": {"host": "local", "port": 9312, "proxy": "socks5://127.0.0.1:9050"},
        },
        "search": {"profile": "research", "tor_profile": "research-tor",
                   "local_profile": "local-research", "local_tor_profile": "local-research-tor", **search},
    }


@pytest.fixture
def clean_env(monkeypatch):
    """No .env, no SCOUT_WHERE, no alternative config file: the test decides."""
    monkeypatch.setattr("scout.browser.config.load_dotenv", lambda *a, **kw: False)
    monkeypatch.setattr("scout.config.load_dotenv", lambda *a, **kw: False, raising=False)
    monkeypatch.delenv("SCOUT_WHERE", raising=False)
    monkeypatch.delenv("SCOUT_BROWSERS_FILE", raising=False)


# -- the where switch ---------------------------------------------------------

def test_where_defaults_to_vm():
    cfg = parse_browser_config(_raw())
    assert cfg.search.where == "vm"
    assert cfg.search.profile == "research"
    assert cfg.search.tor_profile == "research-tor"


def test_where_local_picks_the_local_profiles():
    cfg = parse_browser_config(_raw(where="local"))
    assert cfg.search.where == "local"
    assert cfg.search.profile == "local-research"
    assert cfg.search.tor_profile == "local-research-tor"


def test_where_local_needs_a_local_profile():
    raw = _raw(where="local")
    del raw["search"]["local_profile"]
    with pytest.raises(ConfigError, match="local_profile"):
        parse_browser_config(raw)


def test_local_profile_must_be_on_a_local_host():
    with pytest.raises(ConfigError, match="must be on a local host"):
        parse_browser_config(_raw(where="local", local_profile="research"))
    with pytest.raises(ConfigError, match="must be on a local host"):
        parse_browser_config(_raw(where="local", local_tor_profile="research-tor"))


def test_local_profile_must_exist():
    with pytest.raises(ConfigError, match="local_profile"):
        parse_browser_config(_raw(where="local", local_profile="nope"))


def test_where_is_validated():
    with pytest.raises(ConfigError, match="SCOUT_WHERE"):
        parse_browser_config(_raw(where="bogus"))


def test_where_local_without_a_tor_profile_cannot_force_tor():
    raw = _raw(where="local", tor="on")
    del raw["search"]["local_tor_profile"]
    with pytest.raises(ConfigError, match="tor_profile"):
        parse_browser_config(raw)
    # Without tor: on it is fine: the Tor profile is simply unset.
    del raw["search"]["tor"]
    assert parse_browser_config(raw).search.tor_profile is None


def test_where_vm_ignores_the_local_profiles():
    cfg = parse_browser_config(_raw(where="vm", tor="on"))
    assert cfg.search.profile == "research" and cfg.search.tor_profile == "research-tor"


# -- the shipped config -------------------------------------------------------

def test_shipped_config_local_mode(clean_env, monkeypatch):
    monkeypatch.setenv("SCOUT_WHERE", "local")
    cfg = load_browser_config()
    assert cfg.search.where == "local"
    assert cfg.search.profile == "local-research"
    assert cfg.search.tor_profile == "local-research-tor"
    assert cfg.profile(cfg.search.profile).host.kind == "local"
    assert cfg.profile(cfg.search.tor_profile).host.kind == "local"


def test_shipped_config_default_is_the_vm(clean_env):
    cfg = load_browser_config()
    assert cfg.search.where == "vm"
    assert cfg.search.profile == "research"
    assert cfg.search.tor_profile == "research-tor"


# -- the search layer picks the local profiles --------------------------------

def test_resolve_tor_in_local_mode(clean_env, monkeypatch):
    monkeypatch.setenv("SCOUT_WHERE", "local")
    cfg = load_browser_config()
    assert resolve_tor(cfg, True) == ("local-research-tor", True)
    assert resolve_tor(cfg, False) == ("local-research", False)


# -- the CLI ------------------------------------------------------------------

def test_cli_list_is_unaffected(clean_env, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SCOUT_TASKS_DIR", str(tmp_path))
    from scout import cli
    assert cli.main(["list"]) == 0
    assert "SCOUT_WHERE" not in os.environ
    capsys.readouterr()


def test_cli_where_sets_the_environment(clean_env, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SCOUT_TASKS_DIR", str(tmp_path))

    class Sentinel(Exception):
        pass

    class StubScout:
        def __init__(self, rec, **kwargs):
            self.rec = rec

        def run(self):
            raise Sentinel()

    import scout.agent
    monkeypatch.setattr(scout.agent, "Scout", StubScout)
    from scout import cli
    try:
        with pytest.raises(Sentinel):
            cli.main(["run", "--where", "local", "--no-tor", "find something"])
        assert os.environ["SCOUT_WHERE"] == "local"
    finally:
        monkeypatch.delenv("SCOUT_WHERE", raising=False)   # never leak into later tests
    capsys.readouterr()
