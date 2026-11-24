# Rigor for open-ended research (plan)

Status (2026-11-24): **`standard` and `thorough` work** (RG1–RG6 built); RG7–RG10 and gate G4 to do. Packages RG1–RG10 and gate G4 in [ROADMAP §3](ROADMAP.md#rigor--open-ended-research).

## 1. The problem

On a concrete task (find this paper, this DOI, this file) "done" is obvious. On an open-ended question ("what is
known about X?", "which options exist for Y, and how do they compare?") Scout stops when the planner feels done,
and the prompt pushes it to stop early ("two or three searches are usually enough", "stop as soon as the
objective is met"). What exists today keeps Scout **honest**, not **thorough**:

| Today | What it guarantees | What it does not |
| --- | --- | --- |
| Budgets (40 steps, 45 min), wrap-up nudge, stuck reflection | The task ends, and says what is missing | Nothing about whether enough was looked at |
| Finish challenge | A finish right after failures is questioned once | A finish after one easy success is never questioned |
| Report checklist | Each part is marked done / partial / not done | "Done" is self-assessed |
| Claim verification (`scout/verify.py`) | Claims match the **saved library items** | One weak source passes; pages read but not saved are not evidence at all; disagreement between sources is not looked for |
| Eval set | 20 tasks with a concrete right answer | Open-ended quality is not measured |

## 2. The design: rigor as a per-task setting

`SCOUT_RIGOR=quick|standard|thorough` in `.env`; `python -m scout run/resume --rigor …` overrides for one task;
stored in the task record (`rec.state["rigor"]`) so a resumed task keeps its level. Like `--where` and `--tor`.

| Level | For | Behaviour |
| --- | --- | --- |
| `quick` | Concrete lookups | **Today's behaviour, unchanged** (keeps G1–G3 comparable). The default until G4 passes |
| `standard` | Most questions | `quick` plus the evidence store (RG1), per-claim corroboration (RG2) and confidence computed in code (RG5). No extra searching is required, but the report says plainly which findings rest on one source |
| `thorough` | Open-ended research | `standard` plus the sufficiency rules checked in code before finishing (RG3), a deliberate search for disagreement (RG4), facets split into sub-agents, and a larger budget (80 steps, 90 min) |

The principle: **the model proposes, code checks.** Sufficiency is measured from the task record (sources,
domains, verification results), never self-reported. A finish that misses a rule is refused with what is
missing, like the finish challenge; after the budget runs out, Scout finishes anyway with an honest report
that names the unmet rules (R7: honest completion beats a stuck task).

**Using it now:** `python -m scout run --rigor standard "…"` (or `SCOUT_RIGOR=standard` in `.env`). The report
then ends with a findings table: each finding, its confidence (high / medium / low, computed from the number of
independent supporting sources and any contradiction), the sources by site, and which findings rest on one source
or are contradicted. The pages read are in the task folder under `evidence/`. `--rigor thorough` adds the finish rules (`scout/sufficiency.py`): at least 5 independent sites read, at least one
counter-search (`purpose: "counter"`), the last 2 searches finding little new (at most a quarter of their
results new to the task; sub-agents' searches count), and every finding backed by two
independent sources. A finish that misses one is sent back with what is missing (at most twice, and not in the
last 4 steps); the report ends with which rules were met. The budget becomes 80 steps and 90 minutes unless given.
Sub-agents inherit the level's evidence and checks, not the finish rules (the main task applies them).

**Live check (2026-11-24):** an open-ended question at `standard` ("hydrogen in salt caverns: where, and the
risks?") read pages from 8 sites and wrote a confident-looking report, then ran out of its 20-step budget. The
check on that report (15 sources, 16 claims, 69 s, the real model) found **every claim supported, but each by
one site only** (a library item and the page it came from count once): 16 of 16 findings medium, none
corroborated. Reading many sites is not the same as corroborating each finding; that is what RG3 enforces at
`thorough`. The run also showed that a budget-ended report skipped the check; it is now checked too, and claims
it cannot fix are named.

**Live check at `thorough` (2026-11-24, same question, `--max-steps 50`):** 51 steps in 67 minutes, 4 sub-agents,
19 searches by the main task (5 of them counter-searches), 43 sources (33 independent), 32 library items.
The rules worked as designed: breadth and disagreement were met; the finish was sent back twice for saturation
(steps 34 and 45), then the budget ended and the report said which rules were not met (saturation,
corroboration). 7 of 16 findings were corroborated by ≥ 2 independent sources (0 of 16 at `standard`); the report
gained a section on the disagreements found (industry "proven and sealed" framing against experiments on cyclic
cracking and measured micro-leakage; cost only cheapest at high utilisation). Three fixes came out of the run:

- **Papers behind `doi.org` counted as one site.** Every library item linked by its DOI had the registrable
  domain `doi.org`. Independence now uses `source_key()`: the DOI or arXiv id behind a resolver link, the
  registrable domain otherwise (a preprint and its journal version are still caught by the near-copy check).
  Recomputed on the live run: one more finding becomes high.
- **Sub-agents' searches were not counted.** Their results looked new to the main task, so saturation could
  not be reached; and their counter-searches did not count. Their search logs are now merged into the parent's.
- **"Nothing new at all" is never reached on papers.** Every paper search turned up at least 3 new papers (a long
  tail). Saturation now means at most a quarter of the results are new.

## 3. Packages

### RG1 — Evidence store (C)

Pages Scout reads become evidence, without going into the library. Every `fetch_page` and `browser_read` result
is saved in the task folder (`evidence/<n>.md`: URL, final URL, title, fetched-at, sha256 of the text, the
Markdown), and logged in `rec.state["evidence"]`. Library items (with their PDFs) stay evidence as today. The
library is unchanged: only what the objective asks for is saved there.

### RG2 — Corroboration in verification (C)

`verify_report` returns, per claim, **every** source that supports it (`"sources": ["S1", "S4"]`), not just one,
and covers the evidence store as well as the library. Batched: more than 12 sources → several checker calls
(reasoning off, ~50 s each). Independence is computed in code (RG6): two sources count as independent only if
their registrable domains differ **and** their texts are not near-copies (a syndicated press release on five
sites is one source).

### RG3 — Sufficiency rules before finishing (C)

Checked in code when the planner calls `finish` at `thorough`:

1. **Corroboration:** every key finding has ≥ 2 independent supporting sources, or is labelled single-source in
   the report.
2. **Breadth:** at least N independent domains read (default 5) and at least 2 differently phrased searches per
   checklist part.
3. **Saturation:** the last 2 searches added no new domain and no new supported claim (cheap proxy for "new
   searches stop turning up anything new").
4. **Disagreement looked for:** at least one counter-search ran (RG4).
5. **Unsupported claims fixed:** none left unsupported (today's verification loop).

A miss returns the list of unmet rules and what would satisfy each ("finding 3 rests on one source: look for
another, or label it"). At most two refusals per task, so a question with genuinely little evidence ends with
an honest report instead of looping.

### RG4 — Counter-evidence search (C)

`web_search` and `search_papers` get an optional `purpose` argument (`"main" | "counter"`). The `thorough`
prompt asks for at least one search per key finding phrased to find disagreement, criticism, replication
failures or newer contradicting data. RG3 rule 4 counts `purpose: "counter"` calls in the record. Conflicts
found go into the report's conflicts section, with both sides' sources.

### RG5 — Report with computed confidence (C)

After verification, code appends a findings table: finding, confidence, sources, conflicts. Confidence comes
from the record, not the model: **high** = ≥ 2 independent supporting sources and no conflict; **medium** = one
supporting source, or partial support from several; **low** = partial support only, or a conflict unresolved;
**unsupported** claims are removed or rewritten first. Then "what could not be settled".

### RG6 — Independence helpers (S)

Delegated to Strata (mechanical, clearly testable): `registrable_domain(url)` (eTLD+1, with a bundled public
suffix list snapshot; `co.uk`, `github.io`, `blogspot.com` handled), `near_duplicate(a, b)` (word 5-shingles,
Jaccard ≥ 0.6) and `independent_groups(sources)`. Spec states the intent: undercounting independence is safe,
overcounting is the bug.

### RG7 — Open-ended eval tasks and checks (S for the check code, C for the tasks)

New tasks o01–o06 (new ids, so G1–G3 stay comparable), run at `thorough`:

- questions with a known set of key facts (`report_contains_any` per fact, from a fixed answer key written in
  advance);
- one **single-source trap**: the top search result is outdated or wrong; a pass needs the corrected fact and
  the conflict reported;
- one **controversy**: a pass needs both sides, each with its sources;
- one with **little evidence**: a pass needs low-confidence labels and an honest "could not be settled", not
  invented certainty.

New check kinds (mechanical, delegated): `min_independent_sources`, `findings_have_confidence`,
`counter_search_done`, `report_mentions_conflict`. No model-graded rubric: the grader would be the same model
as the agent.

### Long-running research: sessions, compaction, schedules (RG8–RG10)

Some questions deserve hours, not minutes: "follow what is published on X", "build a full picture of Y". A
**campaign** is one task that runs in **sessions**: each session has its own time box, ends paused with notes,
and the next session continues from them, the same day or the next.

**RG8 — Sessions with a time box (C).** `python -m scout run --session 2h …` (and `resume --session …`): the
session's own deadline; at its end Scout writes **progress notes** (what is covered, what each checklist part
still needs, the open leads, the searches done) and sets the status `paused`, not `done`. `resume` starts the
next session. A campaign ends when the sufficiency rules (RG3) are met, when new sessions stop finding anything
(saturation over two sessions), at `--until <date>` or `--max-sessions N`, or when the user stops it. Each
session's report is a digest of what it added; the last one is the full report.

**RG9 — Compaction between sessions (C).** A transcript that grows for days would overflow the model's context.
A new session starts from a fresh transcript: the objective, the progress notes, the evidence index (RG1: titles,
URLs, what each supports) and the library items. Nothing is lost: the evidence and the old transcripts stay in
the task folder. Later sessions look for what is new: date filters on searches, and evidence and library
deduplication by URL and hash.

**RG10 — Schedules (C).** `python -m scout schedule <task id> --daily 09:00 --session 2h [--until …]` installs a
per-user launchd agent on macOS (a cron line on Linux) that runs `python -m scout resume <id> --session 2h`;
`schedule --list` and `schedule --remove <id>` manage them. Installing a schedule is always the user's own
command: Scout never schedules itself. An unattended session keeps every gate: the Tor choice is fixed in the
task record when the campaign starts; approvals follow `SCOUT_APPROVALS` (`ask` with no terminal denies, as
today); a needed hand-off pauses the campaign as `needs_human` and later sessions wait for it. Each session
first checks that Strata and the browser host answer; if not, it skips and tries next time (R6). The Mac must be
awake (launchd runs a missed job at the next wake).

## 4. Gate G4

- o01–o06 at `thorough`: pass rate ≥ 75 % over 3 repeats.
- The 20 existing tasks at `quick`: no regression (≥ the G3 rate minus noise).
- Recorded: time and steps per level, so the cost of rigor is known.
- A campaign test: one open-ended task over three short sessions (`--session 10m`): it pauses with notes,
  resumes from a compacted transcript, adds new evidence each time and ends by saturation or `--max-sessions`.

Then decide the default level from the numbers (likely `standard`).

## 5. Cost and limits

- `thorough` tasks will take roughly 2–3× as long: more searches, more pages, verification over more sources.
  Strata's ~2 parallel requests cap the gain from sub-agents.
- Domain independence is a proxy: two sites can share an origin that neither cites. Near-copy detection catches
  syndication, not paraphrase.
- Saturation by new domains and claims is a proxy for "nothing new to learn"; a narrow web on the topic will
  saturate early, and the report says so.
- The model still writes the findings; code checks the evidence behind them. It reduces thin research; it does
  not make the model's judgment perfect.

## 6. Order of work

RG6 (Strata, in the background) ∥ RG1 → RG2 → RG5 (then `standard` works) → RG4 → RG3 (`thorough`) → RG8 →
RG9 → RG10 → RG7 → G4.
