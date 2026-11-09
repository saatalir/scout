# Online Scout

A local-first web agent. It researches a topic and downloads papers and data, or navigates a site to finish a task such as an application form.

**Local-first** means two things:

- **Every model call goes to a self-hosted model:** Qwen3.8-Flash-Next served by Strata, on a machine you own.
- **Every web visit goes through a real browser** on a VM you own. There are no search APIs, no API keys and no cloud services.

| Doc | For |
| --- | --- |
| [docs/SETUP.md](docs/SETUP.md) | Building the whole system from scratch, verifying it, and troubleshooting. |
| [docs/STRATA.md](docs/STRATA.md) | Serving Qwen3.8 with Strata on the GPU server and reaching it over the tailnet. |
| [AGENTS.md](AGENTS.md) | Coding agents working in this repository: map, invariants, commands, conventions. |
| [deploy/browser-vm/README.md](deploy/browser-vm/README.md) | Quick reference for the browser VM. |

## Status

| Phase | Scope | State |
| --- | --- | --- |
| 0 — feasibility | Connect to Strata; measure whether the local model can carry each agent role | **Done.** The eval gate passes for planner, browser, vision and extractor ([SETUP §9](docs/SETUP.md#9-reference-results)). |
| Browser layer (Phase 2 work, built early) | Browser profiles on a VM, search with engine fallback, Tor on/off | **Done and verified live.** |
| 1 — Research Scout | Task record, academic APIs, fetch/download with verification, library hand-off, planner loop, CLI, fixed task eval set, parallel sub-agents | **In progress.** Done: everything except parallel sub-agents; baseline run of the task eval in [docs/PHASE1.md](docs/PHASE1.md). |

## Architecture

```
workstation (Scout, evals) ──tailnet──▶ <strata-host>: Strata (OpenAI-compatible /v1)
                           ──SSH─────▶ <browser-vm>: Brave profiles on a virtual screen, tor, live view
```

## Quick start

Do this after the machines exist; see [docs/SETUP.md](docs/SETUP.md) for building them.

```bash
uv venv --python 3.12 .venv && source .venv/bin/activate
uv pip install -e ".[dev]" && playwright install chromium
cp .env.example .env                       # fill in Strata's URL and key, the VM name, the tailnet

python scripts/check_connection.py          # workstation → Strata: chat, tools, images
pytest                                      # everything offline
python scripts/browser_check.py --search "hydrogen storage in gas grids"   # asks: Tor on or off?
python -m evals.run --repeats 3             # the model eval gate

python -m scout run "Find 5 papers since 2023 on hydrogen blending in gas pipelines and download 2 PDFs"
python -m scout list                        # tasks; `show <id>` / `resume <id>`
python -m evals.phase_tasks                 # the fixed task eval set (Phase 1 gate)
```

## Layout

```
scout/config.py         settings from .env (Strata URL, key, pinned model, router)
scout/llm.py            role-based model client: agents ask for roles, never models
scout/agent.py          the orchestrator: planner loop over the tools, approval gates, hand-off to a human
scout/task.py           the task record (saved after every step; pause and resume)
scout/library.py        hand-off to the knowledge library: output contract, deduplication
scout/net.py            HTTP for tools, honouring Tor (SSH tunnel to the VM's Tor)
scout/cli.py            python -m scout run | resume | show | list
scout/browser/          profiles and hosts (config.py), SSH helper + tunnel (hosts.py), CDP sessions (session.py)
scout/tools/search.py   browser search: DuckDuckGo → Brave Search → Google; Tor on/off (resolve_tor, web_search)
scout/tools/scrub.py    removes location and session data from captured result pages
scout/tools/academic.py tier 1: arXiv, OpenAlex, Crossref (no keys)
scout/tools/web.py      tier 2: fetch page → Markdown; downloads with verification
scripts/                check_connection.py, browser_check.py, render_litellm_config.py
evals/                  run.py, suites/ (model gate); tasks/tasks.yaml + phase_tasks.py (the fixed task eval set); thresholds.yaml
config/                 browsers.yaml (profiles, search, Tor), litellm.template.yaml
deploy/browser-vm/      setup.sh and the scout-browser helper for the browser VM
deploy/strata-host/     llama-swap template (only if a role ever needs a second engine)
tests/                  offline tests: browser layer, simulated agent environments, smoke tests vs. a mock server
docs/SETUP.md           the setup guide
docs/STRATA.md          serving the model with Strata and publishing it on the tailnet
docs/PHASE1.md          Phase 1: how a task runs, the library contract, the task eval set and its results
results/                eval output (not committed)
tasks/, library-inbox/  task records and the default library inbox (not committed)
```

## Evals

```bash
python -m evals.run --repeats 3                       # the gate (~15 min with a warm Strata)
python -m evals.run --suites tool_calling,agent_loop  # a subset
```

Results go to `results/<timestamp>/`:

- `summary.md`: the gate table, the suite table and per-case detail.
- `results.jsonl`: one row per case, with traces.
- `meta.json`: run settings.

| Suite | Measures | Decides role |
| --- | --- | --- |
| `tool_calling` | Right tool, schema-valid args, exact values, Greek input, approval before submit, ignoring a prompt injection in a fetched page | planner, browser |
| `agent_loop` | Two simulated multi-step tasks: a job-application portal and a literature search, with distractors, validation and an approval gate | planner, browser |
| `extraction` | Page text to JSON, field by field, including a Greek dataset page | extractor |
| `vision` | Reading text, comparing table values and click grounding on screenshots rendered at 1280×800 | vision |
| `speed` | Time to first token, decode and prefill rates at 8k and 24k context | step budgets |

The thresholds live in `evals/thresholds.yaml`.
