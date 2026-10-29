# Engineer-spec list results

Processed specifications: 2. All source expectations are estimates, not ground truth.
A search failure within budget is not proof of infeasibility. No failed task is scored as a zero-cost success.
Null targets use the agent's disclosed production defaults, not unconstrained optimization. Results for those rows are conditional on those additional default constraints.
Source text is parsed before search and checked against independent YAML quantities/objective. Mismatched or unresolved requests are not simulated. Intake is shared between search arms; each search has a cold independent cache. Export is excluded and approval is automatic.

| Category | Specs | Accepted intake | Clarification | Intake errors/mismatches |
|---|---:|---:|---:|---:|
| easy | 2 | 2 | 0 | 0 |
| medium | 0 | 0 | 0 | 0 |
| hard | 0 | 0 | 0 | 0 |
| objective | 0 | 0 | 0 | 0 |
| infeasible | 0 | 0 | 0 | 0 |
| parser | 0 | 0 | 0 | 0 |

| Strategy | Verified designs / attempted | Simulations | Hard-check failures | Judge mean /4 |
|---|---:|---:|---:|---:|
| agent | 2 / 2 | 78 | 0 | 3.000 |

| Spec | Expectation (not oracle) | Intake | Search outcomes |
|---|---|---|---|
| E01 | feasible | accepted | agent: pass, 40 simulations |
| E02 | feasible | accepted | agent: pass, 38 simulations |

Detailed qualitative assessments and measured alternatives: report.md. Per-spec intake, assumptions and independent contract checks: spec_results.json. Per-task measurements, model/judge usage, failures and traces: results.json and case folders.
