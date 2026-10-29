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
| random | 16 / 20 | 437 | 0 | 2.220 |

| Spec | Expectation (not oracle) | Intake | Search outcomes |
|---|---|---|---|
| E01 | feasible | accepted | random: pass, 15 simulations |
| E02 | feasible | accepted | random: pass, 15 simulations |
| E03 | feasible | accepted | random: pass, 27 simulations |
| E04 | feasible | accepted | random: pass, 15 simulations |
| E05 | feasible | accepted | random: pass, 15 simulations |
| E06 | feasible | accepted | random: pass, 17 simulations |
| E07 | feasible | accepted | random: pass, 15 simulations |
| E08 | feasible | accepted | random: pass, 17 simulations |
| E09 | feasible | accepted | random: pass, 27 simulations |
| E10 | feasible | accepted | random: pass, 15 simulations |
| M01 | feasible | accepted | random: pass, 15 simulations |
| M02 | feasible | accepted | random: pass, 27 simulations |
| M03 | feasible | accepted | random: pass, 16 simulations |
| M04 | feasible | accepted | random: pass, 34 simulations |
| M05 | feasible | accepted | random: pass, 16 simulations |
| M06 | feasible | accepted | random: pass, 28 simulations |
| M07 | feasible | accepted | random: budget_exhausted, 40 simulations |
| M08 | feasible | accepted | random: stalled, 18 simulations |
| M09 | feasible | accepted | random: stalled, 30 simulations |
| M10 | borderline | accepted | random: budget_exhausted, 35 simulations |

Detailed qualitative assessments and measured alternatives: report.md. Per-spec intake, assumptions and independent contract checks: spec_results.json. Per-task measurements, model/judge usage, failures and traces: results.json and case folders.
