# Engineer-spec list results

Processed specifications: 20. All source expectations are estimates, not ground truth.
A search failure within budget is not proof of infeasibility. No failed task is scored as a zero-cost success.
Null targets use the agent's disclosed production defaults, not unconstrained optimization. Results for those rows are conditional on those additional default constraints.
Source text is parsed before search and checked against independent YAML quantities/objective. Mismatched or unresolved requests are not simulated. Intake is shared between search arms; each search has a cold independent cache. Export is excluded and approval is automatic.

| Category | Specs | Accepted intake | Clarification | Intake errors/mismatches |
|---|---:|---:|---:|---:|
| easy | 10 | 10 | 0 | 0 |
| medium | 10 | 10 | 0 | 0 |
| hard | 0 | 0 | 0 | 0 |
| objective | 0 | 0 | 0 | 0 |
| infeasible | 0 | 0 | 0 | 0 |
| parser | 0 | 0 | 0 | 0 |

| Strategy | Verified designs / attempted | Simulations | Hard-check failures | Judge mean /4 |
|---|---:|---:|---:|---:|
| agent | 17 / 20 | 697 | 0 | 3.270 |
| random | 18 / 20 | 514 | 0 | 2.350 |

| Spec | Expectation (not oracle) | Intake | Search outcomes |
|---|---|---|---|
| E01 | feasible | accepted | agent: budget_exhausted, 36 simulations; random: pass, 21 simulations |
| E02 | feasible | accepted | agent: budget_exhausted, 33 simulations; random: pass, 33 simulations |
| E03 | feasible | accepted | agent: pass, 36 simulations; random: pass, 31 simulations |
| E04 | feasible | accepted | agent: pass, 36 simulations; random: pass, 21 simulations |
| E05 | feasible | accepted | agent: budget_exhausted, 40 simulations; random: pass, 21 simulations |
| E06 | feasible | accepted | agent: pass, 37 simulations; random: pass, 17 simulations |
| E07 | feasible | accepted | agent: pass, 35 simulations; random: pass, 33 simulations |
| E08 | feasible | accepted | agent: pass, 37 simulations; random: pass, 24 simulations |
| E09 | feasible | accepted | agent: pass, 36 simulations; random: budget_exhausted, 35 simulations |
| E10 | feasible | accepted | agent: pass, 36 simulations; random: pass, 21 simulations |
| M01 | feasible | accepted | agent: pass, 36 simulations; random: pass, 38 simulations |
| M02 | feasible | accepted | agent: pass, 34 simulations; random: pass, 28 simulations |
| M03 | feasible | accepted | agent: pass, 31 simulations; random: pass, 17 simulations |
| M04 | feasible | accepted | agent: pass, 31 simulations; random: pass, 17 simulations |
| M05 | feasible | accepted | agent: pass, 37 simulations; random: pass, 24 simulations |
| M06 | feasible | accepted | agent: pass, 33 simulations; random: pass, 38 simulations |
| M07 | feasible | accepted | agent: pass, 36 simulations; random: pass, 28 simulations |
| M08 | feasible | accepted | agent: pass, 35 simulations; random: stalled, 18 simulations |
| M09 | feasible | accepted | agent: pass, 31 simulations; random: pass, 28 simulations |
| M10 | borderline | accepted | agent: pass, 31 simulations; random: pass, 21 simulations |

Detailed qualitative assessments and measured alternatives: report.md. Per-spec intake, assumptions and independent contract checks: spec_results.json. Per-task measurements, model/judge usage, failures and traces: results.json and case folders.
