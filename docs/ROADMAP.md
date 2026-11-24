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
3. The spec's tests run there under macOS `sandbox-exec`: no network except localhost, and no writes outside the copy and temp folders. Failures go back to Strata for another round, up to 4. Browser tests work there too: the local Chromium runs with `--no-sandbox` (macOS cannot nest Chromium's own sandbox inside `sandbox-exec`; the outer sandbox still confines it).
4. Claude reviews the files, runs the whole suite, and merges. Nothing lands unreviewed.

> **Why not an autonomous coding agent?** The first version ran opencode (an agentic coder, already set up for Strata) with `--auto` under a deny-by-default permission policy. A policy probe passed, but in the real smoke-test task the agent used absolute paths into the main checkout, wrote files there, and ran shell commands the policy should have denied, including `git stash` in the main checkout. No work was lost; the stash was restored within the run. Since the policy can't be relied on, the model gets no tools at all. Delegated code runs only in the sandbox, and only through the tests.

### What the delegations showed (2026-11-18 to 11-24, eight packages)

| Package | Work | Rounds | Review |
| --- | --- | --- | --- |
| A1–A4 | API adapters with fixture tests | 1–3 | Merged nearly as written |
| B7 | Test fixture pages | 1 | Merged as written |
| B6 | Consent library (a safety rule) | 2 | Tests passed, but its label rule could click "Accept non-essential"; rule rewritten |
| D4 | Playbook promotion | 1 | 361 lines; review changed about 24, all privacy (typed values and task text kept; partly the spec's fault) |
| RG6 | Domain and near-copy helpers, intent stated ("undercounting is safe") | 2 | Correct; a quadratic recomputation and a few over-counting suffixes fixed in review |
| Local mode | Config switch, profiles, CLI | 4 | Code right from round 2, about what Claude would write. Rounds 3–4 (most of 55 min) chased a test that already failed in the sandbox; the harness also mangled `.env.example`. Both were harness faults, now fixed |

Strata reliably meets the letter of the spec and the tests. It misses intent the spec leaves unstated
(safety, privacy), never questions a flawed spec, and repeats itself a little. The gap is judgment, not code.

**Rules for delegating:**

1. **Delegate by kind:** mechanical work with an obvious test (adapters, parsers, fixtures, glue, config).
   Keep gates, Tor, approvals, privacy and design with Claude: reviewing them costs as much as writing them.
2. **Delegate by size:** the spec and the review cost Claude about the same whatever the size; below about
   150 lines, writing it directly is as cheap. Delegate small work only to save Claude's session budget.
3. **State intent, not just the API:** say what must never happen ("never keep a typed value; it may be the
   user's phone number"). Both safety misses came from intent the spec left out.
4. **Write the key tests in the spec:** the tests are where quality lives; Strata hits the target it is given.
5. **Review by risk:** read the safety- and privacy-relevant lines closely, skim the rest.
6. **Harness:** tests that fail on the unchanged copy are recorded as `failing_before` and ignored, and Strata
   is told not to chase them; dotfile paths are kept.

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
| A1 | Semantic Scholar adapter (Graph API, no key, rate-limit aware) | **S** | R-core | Fixture tests and one live call | **Done** (Strata, 1 round). Live: keyless requests are often rate-limited (429), so it's a fallback source |
| A2 | PubMed adapter (E-utilities esearch + esummary, no key) | **S** | R-core | Fixture tests and one live call | **Done** (Strata, 1 round); live OK |
| A3 | Zenodo adapter (records API: datasets and papers with file links) | **S** | R-core | Fixture tests and one live call | **Done** (Strata, 3 rounds, tests unchanged); live OK |
| A4 | Open-data portals: CKAN `package_search` (several government portals); Eurostat later | **S** | R-core | Fixture tests and one live call | **Done** (Strata). Live checks led Claude to swap data.gov (no longer CKAN) for Canada and fix opendata.swiss's multilingual fields |
| SA | Parallel research sub-agents: a `research_subtasks` tool, about 2 concurrent (Strata answers ~2 requests in parallel), each with its own budget, returning summaries and item paths; failures retried or reported | C | R-loop | Tests with a fake model; r02 faster than the 479 s baseline | **Built** (tests pass; the model chose not to split r02 in live runs) |
| VF | Research verification: every claim in the report checked against stored source text (extractor role); unsupported claims fixed or flagged | C | R-loop | Tests: an unsupported claim is caught; eval checks on the report | **Done** (works live; reasoning off for the check: ~50 s; not counted as an agent step) |
| LIB | Point `SCOUT_LIBRARY_DIR` at the real knowledge-library inbox; confirm the front-matter fields its ingestion needs | **U** + C | — | One real item ingested by the library | **Deferred** (your call, 2026-11-22): the capability is ready; set `SCOUT_LIBRARY_DIR` to the inbox when you want it. Until then items go to `./library-inbox` |
| G1 | Phase 1 gate: all phase-1 tasks, 3 repeats, plus new resilience tasks (r11–r13 below) | C | all above | Pass rate ≥ threshold (set from baselines) | **Passed (2026-11-18):** 96 % (43/45) over 3 repeats, threshold 75 %; the two failures were step budgets (r05, r11), r11's fixed (PHASE1 §6) |

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
| B2 | Browser tools: `open`, `read`, `click`, `type`, `select`, `scroll`, `submit` (gated), `browser_download` (fetched back from the VM), `screenshot` | C | B1 | Tests on local fixture pages (local-test profile) | **Built**, incl. `browser_download` (the browser's own download; a remote browser saves it on the VM, set over CDP, and Scout copies it back over SSH; verified live on the VM's Brave) and `browser_screenshot` |
| B3 | Browser resilience: navigation retries, re-snapshot after DOM changes, re-finding stale elements, cookie banners (reject non-essential), popups and new tabs, slow pages | C | B2, R-core | Fixture pages that misbehave on purpose | **Built**: retries, re-read, stale ids, banner hint, new tabs and popups followed (`browser_back` returns), JS dialogs answered safely. slow, flaky and changing pages tested with the B7 fixtures |
| B4 | Human gates in the browser: approval before submit, pay or send; sign-in hand-off with the live-view link, then resume when the human says done | C | B2 | f01 and f02 pass | **Built**: gate in code, with the approval modes ask/auto/deny (`SCOUT_APPROVALS`); password refusal. Hand-off carries the live-view link and the browser URL; `scout resume` continues after the person is done |
| B5 | Playbooks: YAML steps per site and task, with "fuzzy" steps sent to the model; traces of successful runs saved for promotion | C | B2 | f03 passes from a playbook | **Built** (`scout/playbooks.py`, `playbooks/`): f01 and f03 playbooks, verified live on the VM up to the approval gate; traces written as `trace.yaml`. f03 from a playbook: 3/3 in G2 |
| B6 | Cookie-banner and consent selector library across common consent platforms | **S** | B3 | Fixture tests | **Done** (Strata, 2 rounds). Review rewrote the label rule (the delegated one could click "Accept non-essential"), added Sourcepoint-by-id, late-banner waits and consent-or-pay exclusions; live: Cookiebot, OneTrust, BBC declined; pay walls left alone (PHASE2 §4) |
| B7 | Browser fixture pages for tests (forms, banners, popups, slow and changing DOM) | **S** | B1 | Used by B2/B3 tests | **Done** (Strata, 1 round, reviewed): `tests/browser_fixtures.py`, 11 pages, 15 runner tests |
| G2 | Phase 2 gate: f01–f03 plus phase-1 tasks | C | B1–B5 | Pass rate ≥ threshold | **Passed** 2026-11-18: 96 % (52/54) over 3 repeats; f01–f03 3/3 each, f01 and f03 from playbooks; r05 1/3 (step check). [PHASE2 §6](PHASE2.md#6-results) |

### Phase 3 — Desktop vision

| ID | Package | Owner | Depends on | Acceptance | State |
| --- | --- | --- | --- | --- | --- |
| D1 | Disposable desktop sandbox on the VM: Docker with Xvfb, a window manager, Chromium, xdotool; noVNC view; started per task, destroyed after; outputs copied out | C (+U for the Docker install) | — | Start, screenshot, act, stop round trip | **Built** (`scout/desktop.py`, `deploy/desktop/`, helper `desktop-*`; rootless podman instead of Docker: no root-equivalent group); verified live on the VM: round trip, live view, an agent task, a vision click → download → copy out, and no access to the VM's loopback ([PHASE3 §6](PHASE3.md#6-results)) |
| D2 | Vision loop: screenshot → vision role → action (click, type, scroll, key) with verification after each action | C | D1 | v01 passes | **Built** (`scout/vision.py`), on the browser page and the desktop; v01 passed live (vision read the canvas labels) |
| D3 | Escalation from the browser tier to the desktop tier when the DOM can't describe the page (canvas, plugins) | C | D2, B2 | v02 passes | **Built**: the page model flags drawn content (`visual`), the prompt escalates browser → vision → desktop; v02 passed live (it needed no vision: the button is a link) |
| D4 | Playbook promotion: repeated successful traces → proposed playbooks for review | C | B5 | A promoted playbook replays f01 | **Built** (`scout/promote.py`, `python -m scout promote`; Strata, 1 round; reviewed: every typed value a param, no objective text, proposals git-ignored). A playbook promoted from the G2 traces replayed f01 live |
| G3 | Phase 3 gate: v01–v02 plus all earlier tasks | C | D1–D4 | Pass rate ≥ threshold | **Passed** 2026-11-18: 97 % (58/60) over 3 repeats; v01, v02 and f01–f03 3/3 each; r04 and r05 1 fail each (step check). [PHASE3 §6](PHASE3.md#6-results) |

### Rigor — open-ended research

Detail and design: [RIGOR.md](RIGOR.md). A per-task setting `--rigor quick|standard|thorough` (`SCOUT_RIGOR`);
`quick` is today's behaviour and stays the default until G4.

| ID | Package | Owner | Depends on | Acceptance | State |
| --- | --- | --- | --- | --- | --- |
| RG1 | Evidence store: pages read are saved in the task folder and count as sources (not library items) | C | — | Tests: fetched and browser-read pages appear as evidence; resume keeps them | **Built** (`scout/evidence.py`; at `standard`/`thorough`; sub-agents' evidence merged into the parent) |
| RG2 | Corroboration in verification: every supporting source per claim, over library items and evidence, batched | C | RG1, RG6 | Tests: a claim backed by two independent sources is counted twice, by two copies once | **Built** (`corroborate_report` in `scout/verify.py`: claims listed once, judged per batch of 12 sources; supports / partial / contradicts) |
| RG3 | Sufficiency rules checked in code before finish at `thorough` (corroboration, breadth, saturation, counter-search, no unsupported claims); at most two refusals | C | RG2, RG4 | Tests: each rule refuses a finish that misses it; the budget still ends the task honestly | **Built** (`scout/sufficiency.py`; cheap rules first, the claim checker only when they pass; results cached per report) |
| RG4 | Counter-evidence searches (`purpose: "counter"`), conflicts reported with both sides | C | — | Tests: the rule counts counter-searches; a conflict reaches the report | **Built** (`purpose` on `web_search` and `search_papers`; searches logged with what was new; contradictions in the findings table) |
| RG5 | Report findings table with confidence computed from the record (high / medium / low) | C | RG2 | Tests: labels follow the source counts, not the model's wording | **Built** (`confidence()`, `findings_table()`; appended to the report) |
| RG6 | Independence helpers: registrable domain (eTLD+1), near-duplicate texts, independent groups | **S** | — | Fixture tests; undercounting is safe, overcounting fails | **Done** (Strata, 2 rounds; `scout/independence.py`). Review: fingerprints computed once per text (was per pair), CDN and tunnel suffixes removed (they could over-count) |
| RG7 | Open-ended eval tasks o01–o06 (answer keys, a single-source trap, a controversy, a thin-evidence question) and their check kinds | S (checks) + C (tasks) | RG3, RG5 | `pytest`; the runner accepts `--rigor` | |
| RG8 | Sessions: `--session 2h` time box; progress notes; status `paused`; a campaign ends by sufficiency, saturation over sessions, `--until`, `--max-sessions` or the user | C | RG1, RG3 | Tests: a session pauses with notes and the next resumes from them | |
| RG9 | Compaction between sessions: a fresh transcript from the notes, the evidence index and the library; later sessions look for what is new | C | RG8 | Tests: the transcript stays bounded over sessions; no evidence lost | |
| RG10 | Schedules: `scout schedule <id> --daily 09:00 --session 2h` (launchd on macOS, cron on Linux), list and remove; only by the user's command; every gate kept unattended | C | RG8 | Tests with a fake launchctl; one live scheduled session | |
| G4 | Rigor gate: o01–o06 at `thorough` ≥ 75 % over 3 repeats; the 20 tasks at `quick` without regression; time and steps per level recorded | C | RG1–RG7 | Pass rate ≥ threshold | |

### Cross-cutting (any time)

| ID | Package | Owner | Notes |
| --- | --- | --- | --- |
| X2 | Task trace files (jsonl per task: every model turn and tool call, with timings) for debugging and promotion | C | Feeds D4 |
| X3 | Model router (LiteLLM) and queue/swap scheduler | — | **Deferred**: every role passes on Strata; reopen if a role ever moves |
| X4 | Strata version pinning plus an eval rerun before any Strata upgrade (design risk "Strata changes often") | C + U | Record the version in eval results |
| X5 | Clean-up of the unused headless VM `scout-browser` on the old VM host (4 GB RAM) | U | Your call |

---

## 4. Order of work

1. X1, then R-core (the base for everything); A1–A4 delegated to Strata in the background while Claude builds R-model, R-loop and R-fallback.
2. SA and VF; G1 baseline. Then LIB with you.
3. Phase 2: B1 → B2 → B3/B4 (B6, B7 delegated) → B5 → G2.
4. Phase 3: D1 (rootless podman on the VM) → D2 → D3 → D4 → G3.
5. Rigor: RG6 (Strata, in the background) ∥ RG1 → RG2 → RG5 → RG4 → RG3 → RG8 → RG9 → RG10 → RG7 → G4.

Each step ends with tests passing, docs updated, and a local commit. Pushes happen when you say so.
