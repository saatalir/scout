# Phase 1 — Research Scout

Phase 1 turns the Phase 0 pieces (the model, browser search) into an agent. Scout takes an objective, researches it with the cheapest tools that work, verifies what it collects, and hands the results to the knowledge library.

This page covers how a task runs, the tools, the library contract, the task eval set, and where Phase 1 stands.

| Design doc item (Phase 1: "tiers 1 and 2, parallel sub-agents, library hand-off") | State |
| --- | --- |
| Orchestrator: one planner owning the objective | **Built:** `scout/agent.py` |
| Task state: record, step log, files, failures, pause/resume | **Built:** `scout/task.py` |
| Tier 1: structured APIs | **Built:** arXiv, OpenAlex, Crossref (`scout/tools/academic.py`). Not yet: Semantic Scholar, PubMed, Zenodo, Kaggle, open-data portals |
| Tier 2: search + fetch-to-Markdown | **Built:** browser web search (Phase 0 layer) + `fetch_page` (trafilatura) |
| Verification after downloads | **Built:** type, size, PDF opens, expected title present |
| Library hand-off: output contract, deduplication, provenance | **Built:** `scout/library.py` (inbox folder; the library's own ingestion picks it up) |
| Human gates | **Built:** `request_approval`, `hand_to_human`; CLI prompts |
| Tor on/off per task | **Built:** asked at task start; applies to browser *and* HTTP (`scout/net.py`) |
| Fixed eval set (15–20 tasks) | **Built:** `evals/tasks/tasks.yaml` (17 tasks) + `evals/phase_tasks.py` |
| Parallel research sub-agents | **Not built yet.** Next. |
| Verification after research ("check every claim against a stored source") | **Not built yet.** |

---

## 1. Running a task

```bash
python -m scout run "Find 5 papers since 2023 on hydrogen blending in gas pipelines; download 2 open-access PDFs"
#   Use Tor for this task (searches and downloads)? [y/N]      ← when SCOUT_TOR=ask
#   [1] search_papers ok (1.2s)
#   [2] download_file ok (0.9s)
#   …
#   Task 20261109-…: done after 14 steps. Tor: off.
#   Saved to ./library-inbox: …

python -m scout list                 # all tasks with status
python -m scout show <task id>       # the step log and report
python -m scout resume <task id>     # continue a paused or unfinished task from its record
```

| Flag | Meaning |
| --- | --- |
| `--tor` / `--no-tor` | Decide Tor for this task without the question |
| `--max-steps N` | Step budget (default 40) |

Approvals (`request_approval`) are asked on the terminal. Without a terminal they are denied.

## 2. How a task runs

```
objective ──▶ TaskRecord.create (tor decided) ──▶ planner loop (role "planner", max 40 steps)
                                                   │  model returns tool calls
                                                   ├─ search_papers ─┐
                                                   ├─ web_search ────┤ results → model (as data, max 7,000 chars each)
                                                   ├─ fetch_page ────┤ every call logged + record saved
                                                   ├─ download_file ─┤
                                                   ├─ save_to_library┘ → library inbox (deduplicated)
                                                   ├─ request_approval → human (y/N)
                                                   ├─ hand_to_human → status needs_human (stop)
                                                   └─ finish(report) → status done
```

| Behaviour | Detail |
| --- | --- |
| Model calls | `max_tokens=2048` per planner turn (the model reasons first) |
| No tool calls | Three model turns in a row without one end the task as `failed` |
| Failing tools | Reported to the model as `{"error": …}`, logged in `failures`, never fatal |
| Step budget | When it runs out, the task ends as `failed` with the work so far kept |
| Saving | The record (including the model conversation) is written atomically after every step, so `resume` continues where it stopped |

## 3. The tools

| Tool | Tier | What it does | Verification / safety |
| --- | --- | --- | --- |
| `search_papers` | 1 | arXiv (`arxiv_id` for an exact paper), OpenAlex (`sort: cited`, citation counts), Crossref (DOIs) | Marks results `already_in_library` |
| `web_search` | 2 | Real-browser search, DuckDuckGo → Brave → Google | Never bypasses bot checks; `needs_human` plus the live-view link |
| `fetch_page` | 2 | Page → Markdown (main text, links, tables) | PDFs and binaries are redirected to `download_file` |
| `download_file` | 2 | Streams to `tasks/<id>/downloads/` | Kind from the file's first bytes; 200 B – 200 MB; HTML instead of a file fails; PDFs must open and contain `expected_title`; deduplicated by URL and hash |
| `save_to_library` | — | One item to the inbox | Refuses unverified downloads; deduplicated by DOI, arXiv id, URL and hash |
| `request_approval` | gate | Asks the human | Default deny without a human |
| `hand_to_human` | gate | Stops the task (`needs_human`) | For sign-ins, CAPTCHAs, 2FA |
| `finish` | — | Ends with the report | Written in the objective's language |

**Tor:** the task's choice applies to everything. Browser searches use the Tor profile, and HTTP requests from the tools go through an SSH tunnel to the VM's Tor SOCKS port, with DNS resolved at the exit. If Tor can't be reached, the task stops (`TorUnavailable`) and never falls back to a direct connection.

**Verified live:**

| Check | Result |
| --- | --- |
| Tor off | `check.torproject.org`: `IsTor=False` |
| Tor on | `IsTor=True`, through a Tor exit |
| OpenAlex through Tor | HTTP 200 |

## 4. The library output contract

One folder per item in `SCOUT_LIBRARY_DIR` (default `./library-inbox`):

```
library-inbox/
  .scout-index.jsonl                         # one line per item: doi, arxiv, url, sha256, task_id, title, year, source
  20261109-attention-is-all-you-need/
    20261109-attention-is-all-you-need.md    # front matter + notes
    1706.03762v7.pdf                          # the original file, when there is one
```

```yaml
---
title: Attention Is All You Need
authors: [Ashish Vaswani, Noam Shazeer, …]
year: 2017
doi: 10.48550/arxiv.1706.03762
arxiv: '1706.03762'
source_url: https://arxiv.org/abs/1706.03762
date_retrieved: '2026-11-09'
task_id: 20261109-204720-download-the-pdf-of-the-paper-at-f706
kind: paper              # paper | dataset | page | other
route: document          # document | video | audio  → which ingestion learner takes it
source: arxiv            # arxiv | openalex | crossref | web
file: 1706.03762v7.pdf
sha256: bdfaa68d…
# optional: licence, cited_by
---
```

**Rules:**

- Identifiers are normalised: lower-case DOIs without a resolver prefix, arXiv ids without a version.
- An item is a duplicate when any identifier matches. The duplicate is not written, and the tool tells the model "already in the library".
- **Open question:** the knowledge library's actual inbox location and any fields its ingestion requires. Point `SCOUT_LIBRARY_DIR` at it and adjust the contract if needed.

## 5. The fixed task eval set

`evals/tasks/tasks.yaml` holds 17 tasks, each tagged with the first phase whose gate includes it:

| Phase | Tasks | Covers |
| --- | --- | --- |
| 1 | r01–r10, d01–d02 | Known-paper PDF, topic search with year filter, DOI lookup, most-cited (OpenAlex), policy page, Greek request and report, duplicate detection, web search for an official page, prompt injection, research over Tor, dataset ZIP, standard PDF |
| 2 | f01–f03 | Form with approval gate, sign-in hand-off, multi-field form (public test forms until real targets are chosen) |
| 3 | v01–v02 | Canvas chart reading, download behind a button |

**Checks are deterministic** and read the task record, the library inbox and the report: `library_has`, `min_items`, `verified_files`, `pdf_title_matches` (reads the stored PDF), `report_contains`, `report_language`, `no_new_files`, `tool_never_called_with`, `approval_before`, `needs_human`, `max_steps`. Every task must also finish.

```bash
python -m evals.phase_tasks                          # all phase-1 tasks
python -m evals.phase_tasks --only r01-known-paper-pdf,r07-duplicate
python -m evals.phase_tasks --skip-needs browser     # without the browser VM
```

**How runs work:**

- Each run is isolated in `results/<ts>-tasks/` with its own `library/`, `tasks/`, `results.jsonl` and `summary.md`.
- Approvals are recorded and auto-approved.
- The gate threshold is `phase1_tasks.pass_rate` in `evals/thresholds.yaml`: proposed 75%, to be set after baselines.

`r09-prompt-injection` reads `evals/fixtures/injection.html` from the public repository's `main` branch, so it only works once that file is pushed.

## 6. Results

Baseline runs on 2026-11-09 with model `qwen3.8-flash-next-unsloth-ud-q4_k_xl` on Strata (direct). These are 11 of the 12 Phase 1 tasks; `r09-prompt-injection` waits until its fixture is on the public branch.

| Task | Run 1: result, steps, time | Run 2 (after fixes): result, steps, time |
| --- | --- | --- |
| r01-known-paper-pdf | pass, 7, 198 s | pass, 5, 82 s |
| r02-topic-search-recent | pass, 25, 655 s | pass, 18, 479 s |
| r03-find-doi | pass, 5, 162 s | pass, 4, 147 s |
| r04-most-cited | **fail** (26 steps > 12), 674 s | pass, 7, 180 s |
| r05-policy-page | **fail** (15 steps > 12), 389 s | pass, 10, 228 s |
| r06-greek-request | **fail** (18 steps > 15), 528 s | pass, 6, 136 s |
| r07-duplicate | pass, 3, 42 s | pass, 3, 36 s |
| r08-web-search-official-page | pass, 7, 132 s | pass, 7, 94 s |
| r10-tor-research (Tor on) | pass, 7, 332 s | pass, 6, 96 s |
| d01-dataset-zip | pass, 5, 127 s | pass, 5, 72 s |
| d02-standard-pdf | pass, 3, 60 s | pass, 3, 52 s |
| **Total** | **8/11 (73%)**, 121 steps, 55 min | **11/11 (100%)**, 74 steps, 27 min |

**What changed between the runs:**

- **OpenAlex search.** Full-text `search` became `title_and_abstract.search`. Run 1's "most cited RAG works" query returned GPT-4 and SciPy first, and the agent spent 22 searches working around it. Now one call returns the right top three.
- **Efficiency rules in the system prompt.** Search metadata is enough to save a paper, so no fetching pages to "confirm". Fetch only when the content is needed, and stop when the objective is met.

Every run-1 failure was over the step budget with correct output. The fixes cut steps by 39% and time by 51% overall.

**Observations to carry forward:**

| Observation | Detail |
| --- | --- |
| Model time dominates | Tool calls take 0.1–2.5 s; a planner turn takes 10–40 s (the model reasons first). Fewer steps is the main speed lever, and parallel sub-agents are the next. |
| Broad topic searches are the slow case | r02 (5 papers, 2 PDFs) still takes 18 steps and about 8 min |
| Tor costs little | The Tor task ran in 96 s over the tunnel |
