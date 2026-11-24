# Rigor for open-ended research (plan)

Status: **planned** (2026-11-24). Packages RG1–RG7 and gate G4 in [ROADMAP §3](ROADMAP.md#rigor--open-ended-research).

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

## 4. Gate G4

- o01–o06 at `thorough`: pass rate ≥ 75 % over 3 repeats.
- The 20 existing tasks at `quick`: no regression (≥ the G3 rate minus noise).
- Recorded: time and steps per level, so the cost of rigor is known.

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

RG6 (Strata, in the background) ∥ RG1 → RG2 → RG5 (then `standard` works) → RG4 → RG3 (`thorough`) → RG7 → G4.
