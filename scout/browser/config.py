"""Browser hosts and profiles, loaded from config/browsers.yaml."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

from ..config import ROOT

DEFAULT_FILE = ROOT / "config" / "browsers.yaml"
BROWSERS = ("brave", "chrome", "chromium")
ENGINES = ("duckduckgo", "brave", "google")
TOR_MODES = ("ask", "on", "off")


@dataclass(frozen=True)
class Host:
    name: str
    kind: str                     # "ssh" | "local"
    address: str | None = None
    user: str | None = None
    helper: str = "scout-browser"
    live_view: str | None = None
    identity_file: str | None = None   # SSH private key; None = ssh's own defaults

    @property
    def ssh_opts(self) -> list[str]:
        if not self.identity_file:
            return []
        return ["-i", os.path.expanduser(self.identity_file), "-o", "IdentitiesOnly=yes"]

    @property
    def target(self) -> str:
        return f"{self.user}@{self.address}" if self.user else str(self.address)


@dataclass(frozen=True)
class Profile:
    name: str
    host: Host
    browser: str = "brave"
    port: int = 0
    headless: bool = False
    window: tuple[int, int] = (1280, 800)
    locale: str = "en-US"
    timezone: str = "UTC"
    proxy: str | None = None
    extensions: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
    stop_on_exit: bool = False


@dataclass(frozen=True)
class SearchConfig:
    profile: str                        # profile for direct (non-Tor) searches
    tor_profile: str | None = None      # profile whose traffic goes through Tor
    tor: str = "ask"                    # default Tor mode: ask | on | off
    engines: tuple[str, ...] = ENGINES
    results_wanted: int = 10
    max_pages_per_engine: int = 3
    pause_seconds: tuple[float, float] = (2.0, 5.0)


@dataclass(frozen=True)
class BrowserConfig:
    hosts: dict[str, Host]
    profiles: dict[str, Profile]
    search: SearchConfig
    source: Path = field(default=DEFAULT_FILE)

    def profile(self, name: str) -> Profile:
        try:
            return self.profiles[name]
        except KeyError:
            raise KeyError(f"No browser profile {name!r} in {self.source}; "
                           f"have {sorted(self.profiles)}") from None


class ConfigError(ValueError):
    pass


_VAR = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}")


def expand_env(value):
    """Replace ${VAR} and ${VAR:-default} in every string of a loaded YAML tree.

    Machine-specific values (VM hostname, tailnet, key path) live in .env, so
    the committed config holds no personal details. An unset variable with no
    default becomes an empty string, which the validation below then reports.
    """
    if isinstance(value, str):
        return _VAR.sub(lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), value)
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    return value


def load_browser_config(path: str | os.PathLike | None = None) -> BrowserConfig:
    load_dotenv(ROOT / ".env", override=False)
    path = Path(path or os.getenv("SCOUT_BROWSERS_FILE") or DEFAULT_FILE)
    raw = expand_env(yaml.safe_load(path.read_text()) or {})
    return parse_browser_config(raw, source=path)


def parse_browser_config(raw: dict, source: Path = DEFAULT_FILE) -> BrowserConfig:
    def err(msg: str) -> ConfigError:
        return ConfigError(f"{source}: {msg}")

    hosts: dict[str, Host] = {}
    for name, h in (raw.get("hosts") or {}).items():
        kind = h.get("kind", "ssh")
        if kind not in ("ssh", "local"):
            raise err(f"host {name}: kind must be ssh or local, got {kind!r}")
        if kind == "ssh" and not h.get("address"):
            raise err(f"host {name}: an ssh host needs an address")
        hosts[name] = Host(name=name, kind=kind, address=h.get("address"), user=h.get("user"),
                           helper=h.get("helper", "scout-browser"), live_view=h.get("live_view"),
                           identity_file=h.get("identity_file"))

    defaults = raw.get("defaults") or {}
    profiles: dict[str, Profile] = {}
    used_ports: dict[tuple[str, int], str] = {}
    for name, p in (raw.get("profiles") or {}).items():
        merged = {**defaults, **(p or {})}
        host_name = merged.get("host")
        if host_name not in hosts:
            raise err(f"profile {name}: unknown host {host_name!r}")
        browser = merged.get("browser", "brave")
        if browser not in BROWSERS:
            raise err(f"profile {name}: browser must be one of {BROWSERS}, got {browser!r}")
        port = int(merged.get("port", 0))
        if port:
            clash = used_ports.get((host_name, port))
            if clash:
                raise err(f"profiles {clash} and {name} both use port {port} on {host_name}")
            used_ports[(host_name, port)] = name
        window = tuple(int(v) for v in merged.get("window", (1280, 800)))
        if len(window) != 2:
            raise err(f"profile {name}: window must be [width, height]")
        profiles[name] = Profile(
            name=name, host=hosts[host_name], browser=browser, port=port,
            headless=bool(merged.get("headless", False)), window=window,  # type: ignore[arg-type]
            locale=merged.get("locale", "en-US"), timezone=merged.get("timezone", "UTC"),
            proxy=merged.get("proxy"), extensions=tuple(merged.get("extensions") or ()),
            flags=tuple(merged.get("flags") or ()), stop_on_exit=bool(merged.get("stop_on_exit", False)))

    s = raw.get("search") or {}
    engines = tuple(s.get("engines", ENGINES))
    bad = [e for e in engines if e not in ENGINES]
    if bad:
        raise err(f"search: unknown engines {bad}; known: {ENGINES}")
    search_profile = s.get("profile", next(iter(profiles), ""))
    if search_profile and search_profile not in profiles:
        raise err(f"search: unknown profile {search_profile!r}")
    tor_profile = s.get("tor_profile") or None
    if tor_profile and tor_profile not in profiles:
        raise err(f"search: unknown tor_profile {tor_profile!r}")
    tor_mode = str(s.get("tor", "ask") or "ask").strip().lower()
    if tor_mode not in TOR_MODES:
        raise err(f"search: tor must be one of {TOR_MODES}, got {tor_mode!r} (check SCOUT_TOR in .env)")
    if tor_mode == "on" and not tor_profile:
        raise err("search: tor is 'on' but no tor_profile is set")
    pause = tuple(float(v) for v in s.get("pause_seconds", (2, 5)))
    search = SearchConfig(profile=search_profile, tor_profile=tor_profile, tor=tor_mode, engines=engines,
                          results_wanted=int(s.get("results_wanted", 10)),
                          max_pages_per_engine=int(s.get("max_pages_per_engine", 3)),
                          pause_seconds=(pause[0], pause[-1]))
    return BrowserConfig(hosts=hosts, profiles=profiles, search=search, source=source)
