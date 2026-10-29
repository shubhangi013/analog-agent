# Evaluation and system evolution

This document records how the circuit agent changed, why each change was made,
and what the measured results showed. It separates deterministic verification
from qualitative model judging and keeps infrastructure failures out of
performance aggregates.

## What the metrics mean

| Metric | Meaning |
|---|---|
| Verified | Tasks with at least one design that passed deterministic DC and AC checks at all required conditions |
| Simulations | Paid simulator executions used by all cases in that run |
| Simulations to first verified design | Descriptive mean over successful cases only; cross-strategy efficiency uses matched cases solved by both arms |
| Judge mean | Qualitative LLM assessment on a four-point scale; it cannot override deterministic checks |
| `+/-` | Population standard deviation across the three reported observations or seeds |

Agent observation numbers are independent model runs. The evaluation seed is
recorded but does not seed the remote model. Random-search runs are
deterministically seeded.

Specification IDs use their category prefix: E = easy, M = medium, H = hard,
O = objective. E/M and H/O denote those groups.

## Evolution summary

| Stage | Main observation | Change | Result |
|---|---|---|---|
| Historical 50-spec run | Agent lost to random on acceptance, especially easy cases | Performed paired failure analysis | Agent 22/45 versus random 28/45 |
| Planner-feedback correction | Near-miss candidates lost their measured progress and DC failure details before later planner calls | Added each candidate's furthest measured stage and per-condition DC failure reason to context | Three E/M observations averaged 16.67/20 versus random 15.33/20 |
| Repeated-run analysis | Typical-pass candidates deferred from corner testing were never reconsidered after admitted candidates failed | Added budget-aware survivor backfilling in worker-limited batches | E/M improved to 17.33/20 and 10/10 medium cases in every observation |
| Held-out validation | Needed evidence that fixes were not tuned only to E/M failures | Ran untouched H01-H10 and O01-O08 three times | Agent 13.00/18 versus random 9.33/18 |

## Current evidence

| Dataset | Agent | Random | Main result |
|---|---:|---:|---|
| Untouched hard/objective | 13.00/18 +/- 1.63 | 9.33/18 +/- 0.94 | Strongest generalization evidence; hard cases 5.33/10 versus 2.00/10 |
| Easy/medium after backfilling | 17.33/20 +/- 0.47 | 15.33/20 +/- 2.49 | Medium reaches 10/10 in every agent observation; easy remains unstable |
| Historical 50-spec run | 22/45 | 28/45 | Baseline that motivated the corrections |

## Stage 1: historical 50-spec baseline

This run predates both planner-feedback correction and survivor backfilling.
Of the 50 specifications, 45 were accepted at intake and searched by both
strategies.

| Metric | Agent | Random |
|---|---:|---:|
| Verified accepted specifications | 22/45 | 28/45 |
| Verified easy specifications | 4/10 | 9/10 |
| Qualitative judge mean | 2.87/4 | 2.10/4 |

### What we observed

- The agent communicated and reasoned better according to the qualitative
  judge, but converted fewer tasks into verified designs.
- Easy performance was the clearest warning: 4/10 for the agent against 9/10
  for random search.
- The result did not support claiming that model-guided search was more
  reliable or more simulation-efficient.

## Stage 2: planner-feedback correction

Paired error analysis found that 9 of 10 selected easy/medium losses reached
near-miss candidates but later planner calls did not receive enough measured
failure information. Hidden DC failures, especially M5 saturation failures,
were repeatedly omitted from the compact context. One additional loss was
caused by budget and corner-test starvation.

### Change

The context builder now reports:

- each candidate's furthest measured stage;
- its DC failure reason at every measured condition;
- the latest compact results table and deterministic verifier failures.

AC measurements remain authoritative for performance ranking. DC feedback
informs the planner but does not fabricate circuit performance.

### Repeated easy/medium results before backfilling

Dataset: E01-E10 and M01-M10.

| Strategy | Run | Verified | Easy | Medium | Simulations | Mean simulations to first verified design | Judge mean |
|---|---:|---:|---:|---:|---:|---:|---:|
| Agent | Observation 1 | 18/20 | 9/10 | 9/10 | 701 | 34.28 | 3.22/4 |
| Agent | Observation 2 | 16/20 | 9/10 | 7/10 | 707 | 34.19 | 3.07/4 |
| Agent | Observation 3 | 16/20 | 9/10 | 7/10 | 707 | 34.31 | 3.14/4 |
| Random | Seed 0 | 18/20 | 9/10 | 9/10 | 514 | 25.50 | 2.36/4 |
| Random | Seed 1 | 16/20 | 10/10 | 6/10 | 437 | 19.62 | 2.22/4 |
| Random | Seed 2 | 12/20 | 9/10 | 3/10 | 487 | 19.42 | 2.09/4 |

| Strategy | Mean verified | Mean simulations | Mean simulations to first verified design | Judge mean |
|---|---:|---:|---:|---:|
| Agent | 16.67/20 +/- 0.94 | 705.00 | 34.26 | 3.14/4 |
| Random | 15.33/20 +/- 2.49 | 479.33 | 21.51 | 2.22/4 |

### What improved

- Easy success rose from the historical 4/10 to 9/10 in all three agent
  observations.
- Agent success became more stable than random success.
- The qualitative judge consistently preferred the agent's engineering
  output.

### What remained weak

- The agent used substantially more simulations.
- Repeated failures still exhausted the budget despite having candidates that
  had passed the typical condition.
- The first 18/20 observation was not representative by itself; two additional
  observations both scored 16/20.

## Stage 3: survivor-backfilling correction

### Failure mechanism

The orchestrator initially selected a small set of typical-pass candidates for
corner testing. If those admitted candidates failed an early corner, deferred
typical-pass candidates were not promoted even when enough simulation budget
remained. The search could then spend the remaining budget generating a new
proposal round instead of completing promising measured candidates.

### Change

The orchestrator now:

1. admits the initial worker-limited survivor batch;
2. tests the hardest remaining condition first;
3. recomputes all-condition capacity after failures;
4. backfills from deferred typical-pass candidates while budget permits;
5. keeps all admission, condition-order, budget and verification decisions in
   deterministic code.

A focused regression test makes the first two admitted candidates fail
`hot_low_supply` and verifies that deferred candidates are then admitted and
completed.

### Easy/medium results after backfilling

| Strategy | Run | Verified | Easy | Medium | Simulations | Mean simulations to first verified design | Judge mean |
|---|---:|---:|---:|---:|---:|---:|---:|
| Agent | Observation 1 | 17/20 | 7/10 | 10/10 | 697 | 33.94 | 3.27/4 |
| Agent | Observation 2 | 18/20 | 8/10 | 10/10 | 689 | 33.72 | 3.18/4 |
| Agent | Observation 3 | 17/20 | 7/10 | 10/10 | 686 | 33.82 | 3.13/4 |
| Random | Seed 0 | 18/20 | 9/10 | 9/10 | 514 | 25.50 | 2.35/4 |
| Random | Seed 1 | 16/20 | 10/10 | 6/10 | 437 | 19.62 | 2.23/4 |
| Random | Seed 2 | 12/20 | 9/10 | 3/10 | 487 | 19.42 | 2.09/4 |

| Strategy | Mean verified | Mean simulations | Mean simulations to first verified design | Judge mean |
|---|---:|---:|---:|---:|
| Agent | 17.33/20 +/- 0.47 | 690.67 | 33.83 | 3.19/4 |
| Random | 15.33/20 +/- 2.49 | 479.33 | 21.51 | 2.22/4 |

Random was rerun on the current code. The post-backfill manifests contain the
current `agent\orchestrator.py` hash, while the pre-backfill manifest contains a
different hash. Random supplies seeded candidates to the same orchestrator,
scheduler, budget and verifier as the agent. Its success and simulation counts
remain identical because none of the 58 random E/M survivor-selection events
required a second backfill batch; only the separately rerun qualitative judge
changed slightly.

### Before-and-after comparison

| Agent metric | Before backfilling | After backfilling | Change |
|---|---:|---:|---:|
| Mean verified | 16.67/20 | 17.33/20 | +0.66 |
| Variation | +/- 0.94 | +/- 0.47 | More stable |
| Mean simulations | 705.00 | 690.67 | -14.33 |
| Mean simulations to first verified design | 34.26 | 33.83 | -0.43 |
| Judge mean | 3.14/4 | 3.19/4 | +0.05 |
| Easy mean | 9.00/10 | 7.33/10 | -1.67 |
| Medium mean | 7.67/10 | 10.00/10 | +2.33 |

### Interpretation

Backfilling produced a modest overall improvement, lower variation and slightly
lower simulation use. The improvement is concentrated in medium cases, which
passed 10/10 in every observation. Easy performance fell to 7-8/10, so this is
not evidence of a universal improvement.

The traces support a budget-allocation hypothesis for the easy regression.
E01 and E02 passed before backfilling near the end of their budgets, at 35-40
simulations. After the change, their deferred candidates were promoted through
additional backfill batches, failed mainly on corner UGF or M5 saturation, and
left too little capacity for another proposal round. E05 already failed before
backfilling and continued to exhaust its budget on M5 saturation. This is a
trace-supported hypothesis rather than a controlled ablation because model
proposals vary between observations. The next search improvement should compare
the expected value of backfilling against reserving budget for a fresh round,
without sacrificing medium and held-out performance.

## Stage 4: untouched hard and objective validation

H01-H10 and O01-O08 were not used to diagnose or implement either fix.

| Strategy | Run | Verified | Hard | Objective | Simulations | Mean simulations to first verified design | Judge mean |
|---|---:|---:|---:|---:|---:|---:|---:|
| Agent | Observation 1 | 13/18 | 5/10 | 8/8 | 691 | 35.31 | 3.14/4 |
| Agent | Observation 2 | 11/18 | 4/10 | 7/8 | 682 | 33.82 | 2.98/4 |
| Agent | Observation 3 | 15/18 | 7/10 | 8/8 | 670 | 33.87 | 3.22/4 |
| Random | Seed 0 | 10/18 | 3/10 | 7/8 | 648 | 24.80 | 2.03/4 |
| Random | Seed 1 | 10/18 | 2/10 | 8/8 | 679 | 21.40 | 2.07/4 |
| Random | Seed 2 | 8/18 | 1/10 | 7/8 | 659 | 22.50 | 1.93/4 |

| Strategy | Mean verified | Hard mean | Objective mean | Mean simulations | Mean simulations to first verified design | Judge mean |
|---|---:|---:|---:|---:|---:|---:|
| Agent | 13.00/18 +/- 1.63 | 5.33/10 | 7.67/8 | 681.00 | 34.33 | 3.11/4 |
| Random | 9.33/18 +/- 0.94 | 2.00/10 | 7.33/8 | 662.00 | 22.90 | 2.01/4 |

### Matched-case simulation cost

Raw first-design means compare different successful case sets. The matched
comparison below includes only case-run pairs solved by both strategies.

| Dataset | Shared solved case-runs | Agent simulations to first design | Random simulations to first design |
|---|---:|---:|---:|
| Easy/medium | 38 | 34.13 | 22.21 |
| Hard/objective | 24 | 33.71 | 23.17 |

Random still reaches a first verified design sooner on the shared solved cases.
For total compute, the difference is large on easy/medium (690.67 versus
479.33 simulations) but small on hard/objective (681.00 versus 662.00).

### What this established

- The improvement story is not limited to the E/M cases used for error
  analysis.
- The clearest agent advantage is on hard feasibility tasks: 5.33/10 versus
  2.00/10.
- Objective-task success is nearly tied.
- Total simulation use is nearly equal on hard/objective, while random is
  substantially cheaper on easy/medium.
- On case-run pairs both strategies solve, random reaches the first verified
  design sooner.
- The agent's demonstrated advantage is verified success rate and qualitative
  engineering output, not time-to-first-design.

## Additional capability evidence

### Compact natural-language intake

The previously failing compact form:

```text
at least 30dB gain, 20MHz bandwidth, 60deg phase margin, 200uW max
```

is now deterministically interpreted as 30 dB gain, 20 MHz UGF, 60 degrees
phase margin and 200 uW maximum power. The original goal text is preserved.

### MCP optimization proof

Run `first-mcp-test-b200-1` produced a five-condition verified OTA using
157/200 simulations:

| Metric | Typical | Worst tested |
|---|---:|---:|
| Gain | 36.04 dB | 35.10 dB |
| UGF | 32.55 MHz | 27.34 MHz |
| Phase margin | 78.66 degrees | 78.32 degrees |
| Power | 46.80 uW | 51.49 uW |

### MCP restart and resume proof

Run `vscode-restart-rehearsal-20261028-3` was interrupted by terminating the
stdio server during optimization. A new server reported the run as
`interrupted`; reissuing the identical request resumed its checkpoint. The run
then passed all five conditions after 36 simulations with five cache hits.

| Worst tested metric | Result |
|---|---:|
| Gain | 35.44 dB |
| UGF | 22.61 MHz |
| Phase margin | 76.87 degrees |
| Power | 63.37 uW |

## Infrastructure exclusions

These attempts are retained as operational evidence but excluded from
performance statistics:

| Attempt | Problem | Resolution |
|---|---|---|
| `agent_repeat1` | Four concurrent cases created up to 16 simulation workers and produced eight `ngspice -v` timeouts | Excluded the attempt and reduced case concurrency |
| `agent_repeat1_retry` E01/E02 | Two transient `ngspice -v` startup timeouts | Replaced only E01/E02 with clean serial evidence from `agent_repeat1_repairs` |
| Post-backfill observation 2, E08 | One `ngspice -v` timeout | Replaced E08 for both strategies with a clean serial repair run |

Proof-quality evaluation now uses `--case-workers 1`. Each search still owns a
four-worker simulation pool.

## Evidence locations

Compact, path-sanitized evidence is published under `results\`. Each evaluation
folder contains `manifest.json` (code hashes, model, simulator version and
settings), `spec_report.md` (per-specification outcomes) and `progress.svg`.
Raw `results.json`, per-case traces and SQLite databases remain in the local
`runs\` tree and are not published. The 50-specification source list is also
not published.

| Evidence | Location |
|---|---|
| Historical 50-spec run and error analysis | `results\eval\specs_50` |
| First post-feedback agent/random run | `results\eval\easy_medium_postfix\seed0` |
| Random seeds 1 and 2 | `results\eval\easy_medium_postfix\seed1`, `seed2` |
| Second agent observation | `results\eval\easy_medium_postfix\agent_repeat1_retry` plus `agent_repeat1_repairs` |
| Third agent observation | `results\eval\easy_medium_postfix\agent_repeat2` |
| Post-backfill E/M runs | `results\eval\post_backfill\easy_medium\run0` through `run2` |
| Post-backfill E08 repair | `results\eval\post_backfill\repairs\easy_medium-e08-run1` |
| Held-out H/O runs | `results\eval\post_backfill\hard_objective\run0` through `run2` |
| MCP optimization proof | `results\mcp\first-mcp-test-b200-1` |
| MCP restart/resume proof | `results\mcp\vscode-restart-rehearsal-20261028-3` |

## Current conclusion

The system has moved from losing to random search on verified acceptance to
beating it across repeated E/M runs and untouched hard/objective runs. The
strongest evidence is improved medium reliability, held-out hard-task success,
and consistently higher qualitative judge scores. Efficiency is mixed: total
compute is nearly equal on held-out hard/objective cases but random is cheaper
on easy/medium and reaches first verification sooner on matched solved cases.

The next improvement should be judged against all three groups:

1. E01-E10, to recover easy reliability;
2. M01-M10, to preserve the current 10/10 medium result;
3. H01-H10 and O01-O08, to prevent tuning only to previously analyzed cases.
