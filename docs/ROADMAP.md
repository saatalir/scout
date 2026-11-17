# Online Scout — Implementation Roadmap

This is the plan for building the rest of Online Scout, derived from the design proposal (phases 0–3, each closed by an eval gate). Each work package has an owner, dependencies, and an acceptance test. Update the **State** column as work lands.

**Core theme: resilience.** Scout must try hard to complete its mission. It retries, finds alternatives, recovers from crashes and outages, and stops only when every reasonable path has been tried. Even then it reports exactly what it did, what it couldn't do, and why. §1 turns that into rules every component follows.

---

## 1. Resilience principles (apply to every package)

| # | Principle | What it means in code |
| --- | --- | --- |
| R1 | **No failure on the first error** | Every external call (model, HTTP, browser, SSH) goes through one retry policy: transient errors (timeouts, connection resets, 429, 5xx) retry with exponential backoff and jitter, honouring `Retry-After`. Permanent errors (400, 401, 403, 404, bad input) fail at once with a clear reason. |
| R2 | **Alternatives before giving up** | Every capability has a fallback chain: academic sources (OpenAlex → Crossref → arXiv → Semantic Scholar → …), PDF locations (OA PDF → arXiv → other OA locations → the landing page's PDF link → the browser), search engines (DuckDuckGo → Brave → Google), and tiers (API → fetch → browser → desktop). |
| R3 | **Bounded effort** | Budgets keep "trying hard" from becoming "hanging": a per-call timeout, a per-step time limit, a task step budget, and a task wall-clock budget. When a budget runs low, Scout escalates or wraps up instead of looping. |
| R4 | **Stuck detection** | Scout notices repeated identical calls, failure streaks, and windows of steps with no progress. It then injects a reflection (what is still missing, what failed, what has not been tried) and makes the model choose a different approach. |
| R5 | **Crash survival** | A checkpoint is written after every step. `resume` continues from the record, and tools are idempotent (dedup by DOI, URL and hash), so redoing a step is harmless. |
| R6 | **Outage survival** | If the model is down, wait with backoff (Strata cold-starts in about 20 min), then pause the task as `waiting` and let it resume. If the browser VM is down, continue with the non-browser tiers and say so. Network blips are covered by R1. |
| R7 | **Honest completion** | The report has a checklist of the objective's parts: done, partly done, or not done, with what was tried. Partial results are always saved. "Done" means verified, not claimed. |
| R8 | **Human as last resort** | Hand off only for credentials, CAPTCHAs, irreversible actions, or when every alternative is exhausted. Waiting for the human is resumable. |

**Safety beats resilience, always.** No principle above may bypass a CAPTCHA, skip an approval gate, enter credentials, or quietly downgrade privacy. If Tor was chosen and is unavailable, the task stops; it never falls back to direct.

---

## 2. Delegation: what Strata builds, what Claude builds

Building with two models saves tokens without lowering quality:

- **Strata** (Qwen3.8 via opencode) builds packages that are **well specified, self-contained and checkable by tests**: adapters for documented APIs, fixtures, test files, and repetitive glue.
- **Claude** builds the **architecture, security-sensitive code and anything hard to verify by tests**: the agent loop, resilience policy, Tor, approvals, browser control, the sandbox. Claude also reviews every delegated change.

**Delegation harness** (`scripts/delegate.py`, package X1). Strata **writes code; it never runs anything**:

1. The harness sends Strata the spec, the files it may change, and read-only context files. Strata replies with complete files in a fixed block format.
2. Only files the spec allows (`files:` globs) are written, into a scratch copy of the tracked files (`../online-scout-delegate/<name>`). Nothing goes into the main checkout.
3. The spec's tests run there under macOS `sandbox-exec`: no network except localhost, and no writes outside the copy and temp folders. Failures go back to Strata for another round, up to 4.
4. Claude reviews the files, runs the whole suite, and merges. Nothing lands unreviewed.

> **Why not an autonomous coding agent?** The first version ran opencode (an agentic coder, already set up for Strata) with `--auto` under a deny-by-default permission policy. A policy probe passed, but in the real smoke-test task the agent used absolute paths into the main checkout, wrote files there, and ran shell commands the policy should have denied, including `git stash` in the main checkout. No work was lost; the stash was restored within the run. Since the policy can't be relied on, the model gets no tools at all. Delegated code runs only in the sandbox, and only through the tests.

The tag **[S]** marks Strata-delegated packages and **[C]** marks Claude's. **[U]** marks something only the user can do.

---

## 3. Work packages

### Phase 1 — Research Scout (finish)

| ID | Package | Owner | Depends on | Acceptance | State |
| --- | --- | --- | --- | --- | --- |
| X1 | Delegation harness (`scripts/delegate.py`) | C | — | A trivial delegated task lands through it; denied commands are refused | **Done** (v2: code-only, sandboxed tests; see §2) |
| R-core | Resilience core: `scout/resilience.py`. Retry policy (R1) with error classification, `Retry-After`, per-host circuit breaker; deadlines (R3) | C | — | Unit tests: transient retried, permanent not, `Retry-After` honoured, breaker opens and closes, deadline enforced | **Done** |
| R-model | Model-call resilience: empty or truncated answers retried with a bigger budget; malformed tool arguments repaired; model outage → wait, then pause as `waiting` (R6) | C | R-core | Tests with a fake model that fails in each way | **Done** |
| R-loop | Agent loop resilience: stuck detection and reflection (R4), budgets and wrap-up (R3), progress checklist and honest report (R7), auto-resume (R5) | C | R-core | Tests: a repeated call triggers reflection; budget exhaustion triggers wrap-up; the report lists unmet parts | **Done** |
| R-fallback | Fallback chains inside tools (R2): academic-source fallback; PDF location chain (OA → arXiv → OpenAlex locations → landing-page link); fetch retries through mirrors | C | R-core | Tests: the first source fails or is empty → the next is used; a PDF found via the landing page | **Done** |
| A1 | Semantic Scholar adapter (Graph API, no key, rate-limit aware) | **S** | R-core | Fixture tests and one live call | Delegated |
| A2 | PubMed adapter (E-utilities esearch + esummary, no key) | **S** | R-core | Fixture tests and one live call | Delegated |
| A3 | Zenodo adapter (records API: datasets and papers with file links) | **S** | R-core | Fixture tests and one live call | Delegated |
| A4 | Open-data portals: CKAN `package_search` (several government portals); Eurostat later | **S** | R-core | Fixture tests and one live call | Delegated |
| SA | Parallel research sub-agents: a `research_subtasks` tool, about 2 concurrent (Strata answers ~2 requests in parallel), each with its own budget, returning summaries and item paths; failures retried or reported | C | R-loop | Tests with a fake model; r02 faster than the 479 s baseline | **Built** (tests pass; live timing in G1) |
| VF | Research verification: every claim in the report checked against stored source text (extractor role); unsupported claims fixed or flagged | C | R-loop | Tests: an unsupported claim is caught; eval checks on the report | **Built** (tests pass) |
| LIB | Point `SCOUT_LIBRARY_DIR` at the real knowledge-library inbox; confirm the front-matter fields its ingestion needs | **U** + C | — | One real item ingested by the library | |
| G1 | Phase 1 gate: all phase-1 tasks, 3 repeats, plus new resilience tasks (r11–r13 below) | C | all above | Pass rate ≥ threshold (set from baselines) | |

**New resilience eval tasks** (added, not changing existing ones; **in `tasks.yaml`**):

| ID | Task | What it proves |
| --- | --- | --- |
| r11 | A paper whose `pdf_url` is dead but which is reachable elsewhere | The fallback chain finds the PDF |
| r12 | A research task with one academic source made unavailable in the run | Source fallback and an honest report |
| r13 | A task interrupted mid-run (process killed) and then resumed | Crash survival with no duplicates |

### Phase 2 — Browser

| ID | Package | Owner | Depends on | Acceptance | State |
| --- | --- | --- | --- | --- | --- |
| B1 | Page model for the agent: interactive elements with stable ids (accessibility tree plus a DOM fallback), text summary, forms | C | — | Fixture pages → correct element lists | **Done** |
| B2 | Browser tools: `open`, `read`, `click`, `type`, `select`, `scroll`, `submit` (gated), `browser_download` (fetched back from the VM), `screenshot` | C | B1 | Tests on local fixture pages (local-test profile) | **Built** except `browser_download` and `screenshot` |
| B3 | Browser resilience: navigation retries, re-snapshot after DOM changes, re-finding stale elements, cookie banners (reject non-essential), popups and new tabs, slow pages | C | B2, R-core | Fixture pages that misbehave on purpose | **Partly**: retries, re-read, stale ids, banner hint; popups and new tabs to do |
| B4 | Human gates in the browser: approval before submit, pay or send; sign-in hand-off with the live-view link, then resume when the human says done | C | B2 | f01 and f02 pass | **Built**: gate in code, with the approval modes ask/auto/deny (`SCOUT_APPROVALS`); password refusal. Resume-after-sign-in to do |
| B5 | Playbooks: YAML steps per site and task, with "fuzzy" steps sent to the model; traces of successful runs saved for promotion | C | B2 | f03 passes from a playbook | |
| B6 | Cookie-banner and consent selector library across common consent platforms | **S** | B3 | Fixture tests | |
| B7 | Browser fixture pages for tests (forms, banners, popups, slow and changing DOM) | **S** | B1 | Used by B2/B3 tests | |
| G2 | Phase 2 gate: f01–f03 plus phase-1 tasks | C | B1–B5 | Pass rate ≥ threshold | |

### Phase 3 — Desktop vision

| ID | Package | Owner | Depends on | Acceptance | State |
| --- | --- | --- | --- | --- | --- |
| D1 | Disposable desktop sandbox on the VM: Docker with Xvfb, a window manager, Chromium, xdotool; noVNC view; started per task, destroyed after; outputs copied out | C (+U for the Docker install) | — | Start, screenshot, act, stop round trip | |
| D2 | Vision loop: screenshot → vision role → action (click, type, scroll, key) with verification after each action | C | D1 | v01 passes | |
| D3 | Escalation from the browser tier to the desktop tier when the DOM can't describe the page (canvas, plugins) | C | D2, B2 | v02 passes | |
| D4 | Playbook promotion: repeated successful traces → proposed playbooks for review | C | B5 | A promoted playbook replays f01 | |
| G3 | Phase 3 gate: v01–v02 plus all earlier tasks | C | D1–D4 | Pass rate ≥ threshold | |

### Cross-cutting (any time)

| ID | Package | Owner | Notes |
| --- | --- | --- | --- |
| X2 | Task trace files (jsonl per task: every model turn and tool call, with timings) for debugging and promotion | C | Feeds D4 |
| X3 | Model router (LiteLLM) and queue/swap scheduler | — | **Deferred**: every role passes on Strata; reopen if a role ever moves |
| X4 | Strata version pinning plus an eval rerun before any Strata upgrade (design risk "Strata changes often") | C + U | Record the version in eval results |
| X5 | Clean-up of the unused headless VM `scout-browser` on lala (4 GB RAM) | U | Your call |

---

## 4. Order of work

1. X1, then R-core (the base for everything); A1–A4 delegated to Strata in the background while Claude builds R-model, R-loop and R-fallback.
2. SA and VF; G1 baseline. Then LIB with you.
3. Phase 2: B1 → B2 → B3/B4 (B6, B7 delegated) → B5 → G2.
4. Phase 3: D1 (needs Docker on the VM) → D2 → D3 → D4 → G3.

Each step ends with tests passing, docs updated, and a local commit. Pushes happen when you say so.
