---
files: ["scout/browser/config.py", "config/browsers.yaml", "scout/cli.py", ".env.example", "tests/test_local_mode.py"]
context: ["scout/browser/hosts.py", "scout/tools/search.py", "scout/net.py", "scout/desktop.py", "tests/test_browser.py"]
tests: ["tests/test_local_mode.py", "tests/test_browser.py"]
---
# Local mode: run every browser operation on this machine instead of the browser VM

Today Scout's browsers run on a Linux VM reached over SSH (profiles `research` and `research-tor` on host
`browser-vm`). Every caller already goes through the **search profiles** in the config (`cfg.search.profile`
and `cfg.search.tor_profile`): search (`resolve_tor` in `scout/tools/search.py`), the agent's browser, the Tor
HTTP client (`tor_proxy_url` in `scout/net.py`, which already handles a profile on a `local` host) and the
desktop (`scout/desktop.py`, which already refuses a `local` host). Launching a browser on a `local` host is
already implemented (`start_local` in `scout/browser/hosts.py`). So local mode is a **choice of which
profile pair the search config points at**. Do not change hosts.py, search.py, net.py, desktop.py or agent.py.

## 1. Config (`scout/browser/config.py`)

- `SearchConfig` gets a new field `where: str = "vm"` (one of `WHERE_MODES = ("vm", "local")`, a new module
  constant).
- The `search:` section gets three new optional keys: `where`, `local_profile`, `local_tor_profile`.
  - `where` is normalised like `tor` (str, strip, lower; empty → `"vm"`). Not in `WHERE_MODES` → `ConfigError`
    whose message names the value and says "check SCOUT_WHERE in .env".
  - `local_profile` / `local_tor_profile`: each, if set, must name a known profile (`ConfigError` otherwise,
    like `tor_profile`), and that profile's host must be of kind `local` (`ConfigError`: "search:
    local_profile <name> must be on a local host").
  - When `where == "local"`: `local_profile` is required (`ConfigError` naming the key if missing), and the
    resulting `SearchConfig.profile` is `local_profile` and `SearchConfig.tor_profile` is `local_tor_profile`
    (may be None). The `tor == "on"` check (no tor_profile → error) must apply to the **resulting**
    tor_profile.
  - When `where == "vm"`: behaviour is exactly as today (`profile`, `tor_profile` from their keys).
- Keep every existing error message and behaviour; existing tests in `tests/test_browser.py` must pass.

## 2. `config/browsers.yaml`

- In `search:` add, with a comment explaining the choice:
  ```yaml
  # Where Scout's browsers run: vm = the browser VM over SSH (the default, isolated);
  # local = this machine (simpler; less isolation; see docs/SETUP.md).
  where: ${SCOUT_WHERE:-vm}
  local_profile: local-research
  local_tor_profile: local-research-tor
  ```
- Add two profiles after `research-tor` (keep `local-test` unchanged):
  - `local-research`: `host: local`, `port: 9311`. Inherits the defaults (Brave, visible window,
    `stop_on_exit: false`, so its data persists under `~/.scout/profiles/local-research`).
  - `local-research-tor`: `host: local`, `port: 9312`, `proxy: socks5://127.0.0.1:9050` (a tor service on
    this machine, e.g. `brew install tor && brew services start tor`), `timezone: UTC`, and the same four
    `flags` as `research-tor`. Comment: if tor is not running, a task with Tor on stops (TorUnavailable); it
    never falls back to direct.

## 3. CLI (`scout/cli.py`)

- `run` and `resume` get `--where {vm,local}` (default None, help: "where the browsers run (default:
  SCOUT_WHERE, else vm)").
- When given, set `os.environ["SCOUT_WHERE"]` to it **right after parsing**, before anything loads the
  browser config. Add `import os` if needed. Do not change anything else in the CLI.

## 4. `.env.example`

Add after `SCOUT_TOR=ask`, with a one-line comment: `SCOUT_WHERE=vm` (vm = browser VM over SSH; local = this
machine).

## 5. Tests (`tests/test_local_mode.py`, new; offline, no browser launched)

Use `parse_browser_config(raw_dict)` (see its signature in config.py; build the dict in the test) and
`load_browser_config()` with `monkeypatch.setenv`. Cover:

1. `where` missing → `"vm"`; profile/tor_profile as configured.
2. `where: local` → `search.profile == "local-research"`, `search.tor_profile == "local-research-tor"`,
   `search.where == "local"`.
3. `where: local` without `local_profile` → `ConfigError` mentioning `local_profile`.
4. `local_profile` naming a profile on an ssh host → `ConfigError`.
5. `where: bogus` → `ConfigError` mentioning `SCOUT_WHERE`.
6. `where: local`, `tor: on`, no `local_tor_profile` → `ConfigError`.
7. The real `config/browsers.yaml` via `load_browser_config()`: with `SCOUT_WHERE=local` the search profiles
   are the two local ones and their host kind is `local`; with `SCOUT_WHERE` unset they are `research` and
   `research-tor`.
8. `resolve_tor(cfg, True)` and `resolve_tor(cfg, False)` from `scout.tools.search` return the local profiles
   in local mode.
9. CLI: `scout.cli.main(["list", ...])` is not affected; for `--where`, call `main(["show", "nonexistent"])`
   is NOT suitable — instead test the parser by monkeypatching: run `main(["run", "--where", "local", "x"])`
   with `scout.agent.Scout` monkeypatched to a stub that raises a sentinel exception, then assert
   `os.environ["SCOUT_WHERE"] == "local"` (use monkeypatch.delenv first so the test cleans up; catch the
   sentinel). If the CLI asks the Tor question before creating Scout, pass `--no-tor`.
