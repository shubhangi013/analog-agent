# Engineer-spec list results

Processed specifications: 18. All source expectations are estimates, not ground truth.
A search failure within budget is not proof of infeasibility. No failed task is scored as a zero-cost success.
Null targets use the agent's disclosed production defaults, not unconstrained optimization. Results for those rows are conditional on those additional default constraints.
Source text is parsed before search and checked against independent YAML quantities/objective. Mismatched or unresolved requests are not simulated. Intake is shared between search arms; each search has a cold independent cache. Export is excluded and approval is automatic.

| Category | Specs | Accepted intake | Clarification | Intake errors/mismatches |
|---|---:|---:|---:|---:|
| easy | 0 | 0 | 0 | 0 |
| medium | 0 | 0 | 0 | 0 |
| hard | 10 | 10 | 0 | 0 |
| objective | 8 | 8 | 0 | 0 |
| infeasible | 0 | 0 | 0 | 0 |
| parser | 0 | 0 | 0 | 0 |

| Strategy | Verified designs / attempted | Simulations | Hard-check failures | Judge mean /4 |
|---|---:|---:|---:|---:|
| agent | 11 / 18 | 682 | 0 | 2.978 |
| random | 10 / 18 | 679 | 0 | 2.067 |

| Spec | Expectation (not oracle) | Intake | Search outcomes |
|---|---|---|---|
| H01 | borderline | accepted | agent: pass, 32 simulations; random: budget_exhausted, 40 simulations |
| H02 | borderline | accepted | agent: pass, 40 simulations; random: budget_exhausted, 40 simulations |
| H03 | likely_infeasible | accepted | agent: budget_exhausted, 35 simulations; random: budget_exhausted, 40 simulations |
| H04 | borderline | accepted | agent: budget_exhausted, 40 simulations; random: budget_exhausted, 40 simulations |
| H05 | borderline | accepted | agent: budget_exhausted, 40 simulations; random: pass, 31 simulations |
| H06 | likely_infeasible | accepted | agent: pass, 31 simulations; random: pass, 30 simulations |
| H07 | borderline | accepted | agent: budget_exhausted, 34 simulations; random: budget_exhausted, 40 simulations |
| H08 | likely_infeasible | accepted | agent: budget_exhausted, 40 simulations; random: stalled, 38 simulations |
| H09 | likely_infeasible | accepted | agent: budget_exhausted, 40 simulations; random: budget_exhausted, 40 simulations |
| H10 | borderline | accepted | agent: pass, 36 simulations; random: stalled, 28 simulations |
| O01 | feasible | accepted | agent: pass, 40 simulations; random: pass, 38 simulations |
| O02 | feasible | accepted | agent: budget_exhausted, 34 simulations; random: pass, 40 simulations |
| O03 | feasible | accepted | agent: pass, 40 simulations; random: pass, 40 simulations |
| O04 | feasible | accepted | agent: pass, 40 simulations; random: pass, 40 simulations |
| O05 | feasible | accepted | agent: pass, 40 simulations; random: pass, 38 simulations |
| O06 | feasible | accepted | agent: pass, 40 simulations; random: pass, 40 simulations |
| O07 | feasible | accepted | agent: pass, 40 simulations; random: pass, 36 simulations |
| O08 | feasible | accepted | agent: pass, 40 simulations; random: pass, 40 simulations |

Detailed qualitative assessments and measured alternatives: report.md. Per-spec intake, assumptions and independent contract checks: spec_results.json. Per-task measurements, model/judge usage, failures and traces: results.json and case folders.
