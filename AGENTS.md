# AGENTS.md — working in this repository

This file is for coding agents. It gives you the facts you need to change this repository safely. For setting up the machines, read [docs/SETUP.md](docs/SETUP.md). For the model server (Strata: launch, publishing on the tailnet, API key), read [docs/STRATA.md](docs/STRATA.md).

## 1. What this is

**Online Scout** is a local-first web agent.

| Component | What it is |
| --- | --- |
| Model | One self-hosted model, `qwen3.8-flash-next-unsloth-ud-q4_k_xl`, served by Strata (OpenAI-compatible `/v1`). It reasons before answering. |
| Browsers | Brave on a Linux VM, driven over CDP through an SSH tunnel. |
| Search | Through those browsers, with no API keys: DuckDuckGo → Brave Search → Google. |
| Tor | A user choice; by default Scout asks first (`SCOUT_TOR=ask`). |

**Phase status:**

- Phase 0 (model feasibility) is done.
- The browser layer is done and verified live. Phase 3 (vision, desktop, playbook promotion): **the gate passed** (97 % over 3 repeats, 2026-11-18); see [docs/PHASE3.md](docs/PHASE3.md). Phase 2 (browser tier): **the gate passed** (96 % over 3 repeats, 2026-11-18); see [docs/PHASE2.md](docs/PHASE2.md).
- Phase 1 (Research Scout): **the gate passed** (96 % over 3 repeats, 2026-11-18); only the real library inbox (LIB) is left. Agent loop, task record, tier 1 APIs, fetch/download, library hand-off, CLI, the fixed task eval set, the resilience layer, parallel sub-agents and claim verification are built. The plan for everything left is [docs/ROADMAP.md](docs/ROADMAP.md); Phase 1 detail is in [docs/PHASE1.md](docs/PHASE1.md); the browser tier (Phase 2) in [docs/PHASE2.md](docs/PHASE2.md).
- **Core theme: resilience.** Scout must try hard to complete its mission, within safety. The principles R1–R8 are in ROADMAP §1.

## 2. Repository map

| Path | Responsibility | Key symbols |
| --- | --- | --- |
| `scout/config.py` | Settings from `.env` | `load_settings()`, `Settings`, `ROOT` |
| `scout/llm.py` | Role-based model client | `LLM.chat(role, messages, tools=…, max_tokens=…) -> ChatResult` (`.text`, `.tool_calls`, `.raw`) |
| `scout/browser/config.py` | Browser hosts, profiles, search settings; `${VAR:-default}` expansion | `load_browser_config()`, `parse_browser_config()`, `expand_env()`, `Host`, `Profile`, `SearchConfig`, `ConfigError` |
| `scout/browser/hosts.py` | Starting browsers: local, or over SSH via the VM helper plus a tunnel | `start_profile()`, `run_helper()`, `helper_start_args()`, `Endpoint`, `BrowserHostError` |
| `scout/browser/session.py` | CDP attach; closes only Scout's own tabs on exit | `open_browser(profile, cfg)` (async context manager) → `BrowserSession` |
| `scout/tools/search.py` | Engines, fallback, Tor choice | `web_search()`, `search_with()`, `resolve_tor()`, `TorChoiceRequired`, `ENGINES`, `SearchOutcome` |
| `scout/tools/scrub.py` | Removes location and session data from captured HTML | `scrub_html()` |
| `scout/agent.py` | Orchestrator: planner loop over the tools; approval gate; hand-off to a human | `Scout(record, llm=, ask_approval=).run()`, `TOOLS`, `SYSTEM`, `run_task()` |
| `scout/task.py` | Task record, saved atomically after every step; pause/resume | `TaskRecord.create/load/log/save`, `tasks_dir()` |
| `scout/library.py` | Knowledge-library output contract and deduplication | `save_item(Item, task_id=, body=, file=)`, `find_existing()`, `read_front_matter()` |
| `scout/net.py` | HTTP client for tools; Tor on = SOCKS over an SSH tunnel to the VM's tor | `http_client(tor)`, `TorUnavailable` |
| `scout/tools/academic.py` | Tier 1: arXiv, OpenAlex, Crossref → one `Paper` shape | `search_papers(client, query, source, …)` |
| `scout/tools/web.py` | Tier 2: page → Markdown; downloads verified by type, size and PDF title | `fetch_page()`, `download_file()`, `pdf_text()` |
| `scout/approvals.py` | Approval policy for irreversible actions: ask / auto / deny, domain allowlist, payments switch | `ApprovalPolicy.from_env()`, `.decide()` |
| `scout/browser/page.py` | Page model: visible interactive elements with stable ids, labels, forms, banners; password values never read | `snapshot(page)`, `PageView.for_model()`, `locator_for()` |
| `scout/browser/consent.py` | B6: declines cookie/consent banners (13 platforms + label search, 10 languages); never accepts, never consent-or-pay; run by the page reader | `reject_consent()`, `is_reject_label()`, `banner_present()` |
| `scout/browser/agent_tools.py` | Browser tools for the agent (open, read, click, type, select, scroll, back, screenshot, download, click_submit) on one session per task; gates in code; follows new tabs and popups; declines confirm dialogs except in an approved click_submit; downloads are caught from the browser, never fetched by Playwright's request API (it would bypass the VM and Tor); a remote browser saves them on the VM (set over CDP) and Scout copies them back over SSH | `BROWSER_TOOLS`, `BrowserRunner` |
| `scout/playbooks.py`, `playbooks/*.yaml` | B5: known site flows as YAML steps (open, type, select, check, click, submit, expect, wait, fuzzy); elements found by label; run through the same browser tools and gates; a failed or fuzzy step hands the page back to the model; traces of successful runs (`trace.yaml` in the task folder) | `run_playbook()`, `matching()`, `find_element()`, `trace_from_actions()` |
| `scout/vision.py` | D2: vision loop on the current screen (browser page or desktop): look, click/type by description, keys, scroll; screenshot check after each action; submit approval, password, CAPTCHA and cookie-accept gates in code | `VisionTools`, `VISION_TOOLS`, `Screen`, `look()`, `locate()` |
| `scout/desktop.py`, `deploy/desktop/` | D1: disposable desktop on the VM (rootless podman, image `scout-desktop`), driven via `scout-browser desktop-*` over SSH; no Tor route (refused when Tor is on); downloads copied out and verified on close | `Desktop`, `DesktopScreen`, `DESKTOP_TOOLS` |
| `scout/promote.py` | D4: traces of a flow that succeeded ≥ 2 times → proposed playbook in `playbooks/proposed/` (inactive, git-ignored) for review; typed values always become params | `promote()`, `propose()`, `flow_key()` |
| `scout/resilience.py` | R1/R3 primitives: transient vs permanent errors, `RetryPolicy`, `Retry-After`, per-host `CircuitBreaker`, `RetryTransport` (under all tool HTTP), `Deadline`, `run_with_time_limit`, `SCOUT_CHAOS_DOWN` fault injection | `is_transient()`, `RetryPolicy.call()`, `RetryTransport` |
| `scout/verify.py` | Claims in the report checked against saved sources (extractor role) | `verify_report(llm, report, files)` |
| `scripts/delegate.py` | Strata writes code from a spec in `docs/delegation/`; tests run sandboxed; Claude reviews and merges | `start/check/merge/drop <name>` |
| `scout/cli.py` | `python -m scout run/resume/show/list/promote`; asks the Tor question and approvals on the terminal | `main()` |
| `evals/tasks/tasks.yaml`, `evals/phase_tasks.py` | The fixed task eval set (20 tasks, phase-tagged; r11–r13 test resilience) and its runner/checks | `python -m evals.phase_tasks` |
| `deploy/browser-vm/scout-browser` | Helper on the VM (stdlib-only Python); one JSON object per call | `start`, `stop`, `status`, `desktop-start/shot/act/files/fetch/stop/list` |
| `deploy/browser-vm/setup.sh` | Idempotent VM setup (root) | — |
| `config/browsers.yaml` | Hosts, profiles (`research`, `research-tor`, `local-test`), search and Tor settings | — |
| `scripts/check_connection.py` | Workstation → Strata layered check | — |
| `scripts/browser_check.py` | Browser layered check; `--search`, `--capture`, `--tor`/`--no-tor` | — |
| `evals/` | Phase 0 eval gate: `run.py`, `core.py`, `suites/*.py`, `thresholds.yaml` | `python -m evals.run` |
| `tests/` | Offline tests; `fake_ssh.py` stands in for ssh; `mock_server.py` is a fake OpenAI server; `browser_fixtures.py` serves misbehaving pages to the local browser | `pytest` |

## 3. Invariants — do not break these

1. **Agents ask for roles, never model names.** Call `LLM.chat("planner" | "browser" | "vision" | "extractor", …)`. Model choice lives only in `.env` (`STRATA_MODEL`, pinned) or the LiteLLM template.
2. **Do not set `max_tokens` below 512** for calls that need an answer. The model reasons first and can return an empty answer with `finish_reason=length`. Treat an empty answer as an error.
3. **Tor is the user's choice.** When `SCOUT_TOR=ask`, ask the user once per task ("search through Tor: on or off?") and pass `tor=True/False` to every `web_search` call in that task. Never default it silently and never catch `TorChoiceRequired` just to continue without Tor.
4. **Never bypass bot checks.** A blocked engine is skipped. If every engine is blocked, return `needs_human` with the live-view URL. Do not add CAPTCHA solving, stealth plugins or fingerprint spoofing to get past blocks.
5. **Irreversible actions go through the approval gate** (`click_submit`, `request_approval` → `scout/approvals.py`). Who approves is the user's setting `SCOUT_APPROVALS`: `ask` (the human, default), `auto` (no human in the loop: Scout approves and records it, limited by `SCOUT_AUTO_APPROVE_DOMAINS`; payments only with `SCOUT_AUTO_APPROVE_PAYMENTS=yes`) or `deny` (dry run). Never bypass the gate in code. **Passwords and CAPTCHAs are hard limits in every mode:** `browser_type` refuses password fields; sign-ins use persistent browser profiles or `hand_to_human`.
6. **DevTools, VNC, noVNC and Tor ports bind to `127.0.0.1` on the VM.** Reach them only through SSH tunnels or `tailscale serve`. Never bind them to `0.0.0.0`.
7. **No personal or machine data in the repository.** Hostnames, the tailnet, users, IP addresses, keys and locations belong in `.env` (git-ignored). `config/browsers.yaml` uses `${VAR:-default}` placeholders. Captured pages go through `scrub_html` (`browser_check.py --capture` does this), and `test_fixtures_are_scrubbed` enforces it.
8. **Scout's SSH key is used with `-i <key> -o IdentitiesOnly=yes -o BatchMode=yes`.** Never add password prompts or interactive SSH to Scout's code paths.
9. **`open_browser` must leave the user's tabs alone.** It closes only pages Scout opened. Remote browsers keep running (`stop_on_exit: false`) so sign-ins persist.
10. **All tool HTTP goes through `scout.net.http_client(tor)`.** Never create an `httpx.Client` in a tool: with Tor on, every request (APIs included) must leave through Tor, and `TorUnavailable` must stop the task, never fall back to direct.
11. **Nothing reaches the library unverified.** `save_to_library` refuses a download whose verification failed; saves are deduplicated by DOI, arXiv id, URL and file hash (`scout/library.py`). Keep the front-matter contract fields stable: the library's ingestion depends on them.
12. **The task record is the source of truth.** Every tool call is logged with `TaskRecord.log`; a task must be resumable from `task.json` alone.
13. **Do not change a task's checks in `evals/tasks/tasks.yaml`** once results exist for it; add a new task instead, so runs stay comparable.
14. **Resilience never overrides safety.** Retries and fallbacks must not bypass a CAPTCHA, skip an approval gate, enter credentials or downgrade Tor to direct. If a safety condition blocks the path, report it or hand off.
15. **External calls are retried centrally.** HTTP: automatically, by `RetryTransport` in `scout.net`. Model: `Scout._model_turn`. Do not add ad-hoc retry loops in tools; add fallbacks (other sources or locations) instead, and report what was tried.
16. **Vision and the desktop keep every gate.** Vision clicks that submit go through the approval gate; passwords are never typed and CAPTCHAs never solved on any screen; the desktop container keeps its isolation flags (no capabilities, read-only, no host loopback, view on 127.0.0.1) and is removed when the task ends.
17. **Never give a coding agent tools on this machine.** Delegate through `scripts/delegate.py`: the model returns files, tests run under `sandbox-exec`, and nothing merges unreviewed. An opencode `--auto` run escaped its policy once (ROADMAP §2).

## 4. Commands

```bash
source .venv/bin/activate
pytest -q                                              # offline; must pass before any commit
python scripts/check_connection.py                     # needs Strata
python scripts/browser_check.py research               # needs the browser VM
python scripts/browser_check.py --no-tor --search "q"  # live search, Tor off (--tor: on)
python scripts/browser_check.py research --capture     # refresh tests/fixtures/search/*_live.html (scrubbed)
python -m evals.run --repeats 3                        # the model gate, ~15 min
```

**Which commands need the network:**

| Needs | Commands |
| --- | --- |
| Nothing (offline) | `pytest`. It uses a local headless Chromium (`playwright install chromium`), `tests/fake_ssh.py` and `tests/mock_server.py`. |
| Strata | `check_connection.py`, `evals.run` |
| The browser VM | `browser_check.py` |

## 5. Making common changes

| Change | Where | Then |
| --- | --- | --- |
| An engine returns 0 results without being blocked | Its `RESULTS` selector or `extract()` in `scout/tools/search.py` | Run `--capture`, then `pytest` (the `test_live_capture_*` tests use the new pages) |
| Add a search engine | A class with `name`, `url()`, `prepare()`, `blocked()`, `extract()`; register it in `ENGINES`; add its name to `ENGINES` in `scout/browser/config.py` | Add a synthetic `<engine>_results.html` and a live capture; extend the tests |
| Add a browser profile | `profiles:` in `config/browsers.yaml`, with a unique port per host | Run `browser_check.py <profile>` |
| Add a profile setting | `Profile` + `parse_browser_config()` in `scout/browser/config.py`; `helper_start_args()` in `hosts.py`; the argument in `deploy/browser-vm/scout-browser` | Rerun `setup.sh` on the VM (it reinstalls the helper); run `pytest` |
| Add a machine-specific value | `.env.example` (placeholder) + `.env` (real), referenced as `${VAR:-default}` | Never hard-code the value |
| Add an agent tool | Schema in `TOOLS` + a branch in `Scout.call()` in `scout/agent.py`; HTTP via the client passed in | Tests with `FakeLLM` and `mock_client` in `tests/test_phase1.py` |
| Add an academic source | A `search_<source>()` returning `Paper` in `scout/tools/academic.py`; add it to `SOURCES` | A mock response in `tests/test_phase1.py` |
| Add an eval case | `evals/suites/<suite>.py` | Run `pytest` (smoke tests run every suite against the mock server), then `evals.run --suites <suite>` |
| Change a gate threshold | `evals/thresholds.yaml` | Record the reason in the commit message |

## 6. Conventions

- Python ≥ 3.11 (3.12 used); `from __future__ import annotations`; dataclasses for plain data.
- Browser code is async (Playwright async API); the model client is sync.
- Scripts print one `OK` / `FAIL` line per layer, in order, so the first failure names the broken layer.
- Errors carry the cause and the fix (e.g. `ConfigError` names the file and the bad key).
- Tests must not need the network, Strata or the VM.
- Keep the docs current. Behaviour changes go into `docs/SETUP.md` (and this file when an invariant or map entry changes).

## 7. Environment variables

`.env.example` lists them all. These are the ones that change behaviour:

| Variable | Values | Effect |
| --- | --- | --- |
| `SCOUT_ROUTER` | `direct` / `litellm` | Whether Scout talks to Strata directly or through LiteLLM |
| `STRATA_MODEL` | model id / `auto` | Pinned model. `auto` takes the first listed model; avoid it. |
| `SCOUT_TOR` | `ask` / `on` / `off` | Default Tor mode for searches (§3 rule 3) |
| `SCOUT_VM_HOST`, `SCOUT_TAILNET`, `SCOUT_VM_USER`, `SCOUT_SSH_KEY` | strings | Where the browser VM is and how Scout logs in |
| `SCOUT_TIMEZONE` | IANA name | Time zone for non-Tor profiles |
| `SCOUT_SCRUB_PLACES`, `SCOUT_SCRUB_REGIONS` | comma lists | What `scrub_html` removes from captures |
| `SCOUT_BROWSERS_FILE` | path | Alternative to `config/browsers.yaml` |
| `SCOUT_LIBRARY_DIR` | path | The knowledge library's inbox (default `./library-inbox`) |
| `SCOUT_TASKS_DIR` | path | Task records (default `./tasks`) |
| `SCOUT_PLAYBOOKS_DIR` | path | Playbooks (default `./playbooks`) |
| `SCOUT_CONSENT_WAIT` | seconds | How long `browser_open` waits for a consent banner injected after load (default 3; tests 0) |
| `SCOUT_APPROVALS` | `ask` / `auto` / `deny` | Who approves irreversible actions (§3 rule 5); `scout run --approvals` overrides per task |
| `SCOUT_AUTO_APPROVE_DOMAINS`, `SCOUT_AUTO_APPROVE_PAYMENTS` | comma list / `yes`/`no` | Limits on `auto` approvals |
| `SCOUT_CHAOS_DOWN` | comma list of hosts | **Tests and evals only:** makes those hosts unreachable (fault injection, eval task r12) |
