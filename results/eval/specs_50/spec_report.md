# Engineer-spec list results

Processed specifications: 50. All source expectations are estimates, not ground truth.
A search failure within budget is not proof of infeasibility. No failed task is scored as a zero-cost success.
Null targets use the agent's disclosed production defaults, not unconstrained optimization. Results for those rows are conditional on those additional default constraints.
Source text is parsed before search and checked against independent YAML quantities/objective. Mismatched or unresolved requests are not simulated. Intake is shared between search arms; each search has a cold independent cache. Export is excluded and approval is automatic.

| Category | Specs | Accepted intake | Clarification | Intake errors/mismatches |
|---|---:|---:|---:|---:|
| easy | 10 | 10 | 0 | 0 |
| medium | 10 | 10 | 0 | 0 |
| hard | 10 | 10 | 0 | 0 |
| objective | 8 | 8 | 0 | 0 |
| infeasible | 6 | 6 | 0 | 0 |
| parser | 6 | 1 | 5 | 0 |

| Strategy | Verified designs / attempted | Simulations | Hard-check failures | Judge mean /4 |
|---|---:|---:|---:|---:|
| agent | 22 / 45 | 1630 | 0 | 2.867 |
| random | 28 / 45 | 1413 | 0 | 2.102 |

| Spec | Expectation (not oracle) | Intake | Search outcomes |
|---|---|---|---|
| E01 | feasible | accepted | agent: budget_exhausted, 40 simulations; random: pass, 21 simulations |
| E02 | feasible | accepted | agent: budget_exhausted, 40 simulations; random: pass, 33 simulations |
| E03 | feasible | accepted | agent: pass, 36 simulations; random: pass, 31 simulations |
| E04 | feasible | accepted | agent: pass, 31 simulations; random: pass, 21 simulations |
| E05 | feasible | accepted | agent: pass, 33 simulations; random: pass, 21 simulations |
| E06 | feasible | accepted | agent: budget_exhausted, 34 simulations; random: pass, 17 simulations |
| E07 | feasible | accepted | agent: budget_exhausted, 40 simulations; random: pass, 33 simulations |
| E08 | feasible | accepted | agent: budget_exhausted, 35 simulations; random: pass, 24 simulations |
| E09 | feasible | accepted | agent: pass, 36 simulations; random: budget_exhausted, 35 simulations |
| E10 | feasible | accepted | agent: budget_exhausted, 36 simulations; random: pass, 21 simulations |
| M01 | feasible | accepted | agent: pass, 31 simulations; random: pass, 38 simulations |
| M02 | feasible | accepted | agent: budget_exhausted, 40 simulations; random: pass, 28 simulations |
| M03 | feasible | accepted | agent: budget_exhausted, 39 simulations; random: pass, 17 simulations |
| M04 | feasible | accepted | agent: pass, 37 simulations; random: pass, 17 simulations |
| M05 | feasible | accepted | agent: budget_exhausted, 35 simulations; random: pass, 24 simulations |
| M06 | feasible | accepted | agent: pass, 35 simulations; random: pass, 38 simulations |
| M07 | feasible | accepted | agent: pass, 30 simulations; random: pass, 28 simulations |
| M08 | feasible | accepted | agent: pass, 35 simulations; random: stalled, 18 simulations |
| M09 | feasible | accepted | agent: pass, 32 simulations; random: pass, 28 simulations |
| M10 | borderline | accepted | agent: budget_exhausted, 37 simulations; random: pass, 21 simulations |
| H01 | borderline | accepted | agent: pass, 36 simulations; random: pass, 35 simulations |
| H02 | borderline | accepted | agent: pass, 40 simulations; random: budget_exhausted, 40 simulations |
| H03 | likely_infeasible | accepted | agent: pass, 31 simulations; random: budget_exhausted, 40 simulations |
| H04 | borderline | accepted | agent: budget_exhausted, 37 simulations; random: budget_exhausted, 40 simulations |
| H05 | borderline | accepted | agent: budget_exhausted, 38 simulations; random: pass, 24 simulations |
| H06 | likely_infeasible | accepted | agent: budget_exhausted, 38 simulations; random: pass, 16 simulations |
| H07 | borderline | accepted | agent: pass, 30 simulations; random: budget_exhausted, 40 simulations |
| H08 | likely_infeasible | accepted | agent: pass, 40 simulations; random: stalled, 39 simulations |
| H09 | likely_infeasible | accepted | agent: budget_exhausted, 40 simulations; random: budget_exhausted, 40 simulations |
| H10 | borderline | accepted | agent: budget_exhausted, 37 simulations; random: budget_exhausted, 40 simulations |
| O01 | feasible | accepted | agent: budget_exhausted, 36 simulations; random: pass, 40 simulations |
| O02 | feasible | accepted | agent: pass, 40 simulations; random: pass, 40 simulations |
| O03 | feasible | accepted | agent: budget_exhausted, 40 simulations; random: pass, 40 simulations |
| O04 | feasible | accepted | agent: pass, 40 simulations; random: pass, 34 simulations |
| O05 | feasible | accepted | agent: pass, 40 simulations; random: pass, 40 simulations |
| O06 | feasible | accepted | agent: pass, 40 simulations; random: budget_exhausted, 40 simulations |
| O07 | feasible | accepted | agent: pass, 40 simulations; random: pass, 40 simulations |
| O08 | feasible | accepted | agent: pass, 40 simulations; random: budget_exhausted, 35 simulations |
| X01 | infeasible | accepted | agent: budget_exhausted, 40 simulations; random: budget_exhausted, 40 simulations |
| X02 | infeasible | accepted | agent: stalled, 26 simulations; random: stalled, 38 simulations |
| X03 | infeasible | accepted | agent: budget_exhausted, 40 simulations; random: budget_exhausted, 40 simulations |
| X04 | infeasible | accepted | agent: budget_exhausted, 40 simulations; random: stalled, 39 simulations |
| X05 | infeasible | accepted | agent: stalled, 18 simulations; random: stalled, 18 simulations |
| X06 | infeasible | accepted | agent: budget_exhausted, 40 simulations; random: budget_exhausted, 40 simulations |
| A01 | clarify | clarification_required | Please clarify: Are the explicit defaults intended as minimum gain, minimum unity-gain bandwidth, minimum phase margin, and maximum DC power requirements? Please restate each with its units and comparison. Does “low-power” mean minimize DC power, or only satisfy a maximum DC power limit? |
| A02 | clarify | clarification_required | Please clarify: What exact minimum gain in dB do you require? “About” does not specify a firm minimum. What minimum unity-gain bandwidth in Hz does “fast” require? Phase margin and DC power are absent from the goal. Should explicit_defaults.pm_deg and explicit_defaults.power_w apply as the minimum phase margin in degrees and maximum DC power in watts, respectively? |
| A03 | feasible | accepted | agent: pass, 31 simulations; random: pass, 21 simulations |
| A04 | feasible | clarification_required | Please clarify: Does “20MHz bandwidth” mean a minimum unity-gain bandwidth? Is “60deg phase margin” a minimum requirement? |
| A05 | clarify | clarification_required | Please clarify: Phase-margin and DC-power requirements are absent from the goal. Should the supplied pm_deg and power_w defaults apply? If so, please state them with units and minimum/maximum comparisons. |
| A06 | clarify | clarification_required | Can you restate this using gain, unity-gain bandwidth, phase margin and power for the supported five-transistor OTA? Other requirements cannot be guaranteed. |

Detailed qualitative assessments and measured alternatives: report.md. Per-spec intake, assumptions and independent contract checks: spec_results.json. Per-task measurements, model/judge usage, failures and traces: results.json and case folders.
