"""Hard evaluation gates; qualitative model scores cannot override these."""

import math
import hashlib
from pathlib import Path

from agent.sim import Candidate
from agent.sim import SimulationResult
from agent.debug_mode import Experiment, intervention
from agent.explain import Explanation, render_explanation
from agent.sim import Condition, parse_transient, render_netlist
from agent.verifier import Targets, verify, verify_dc
from agent.tracer import TraceEvent


def heuristic_checks(result: dict) -> list[dict]:
    if result["mode"] != "optimize":
        events = [TraceEvent.model_validate_json(line)
                  for line in Path(result["trace"]).read_text().splitlines()]
        simulations = [event for event in events if event.type == "simulation"]
        probes = {probe["name"]: SimulationResult.model_validate(probe["result"])
                  for probe in result["probes"]}
        trace_results = {event.fingerprint: event for event in simulations}

        def fixed_bias_matches(probe: dict) -> bool:
            fixed = probe.get("fixed_bias_v")
            if fixed is None:
                return True
            try:
                measured = SimulationResult.model_validate(probe["result"])
                netlist = render_netlist(
                    Candidate.model_validate(probe["candidate"]),
                    Condition.model_validate(probe["result"]["condition"]),
                    measured.analysis, probe["circuit_body"], fixed_bias_v=fixed)
                return (
                    measured.bias_voltage_v is not None
                    and math.isclose(measured.bias_voltage_v, fixed, rel_tol=0, abs_tol=1e-12)
                    and trace_results[probe["fingerprint"]].inputs.get("netlist_sha")
                    == hashlib.sha256(netlist.encode()).hexdigest()
                )
            except (ValueError, TypeError):
                return False

        grounded = bool(probes) and all(
            probe["fingerprint"] in trace_results
            and all(trace_results[probe["fingerprint"]].outputs.get(field) == probe["result"].get(field)
                    for field in ("bias", "nodes", "dc_power_w", "transient", "output_v",
                                  "bias_voltage_v", "tail_current_a"))
            and trace_results[probe["fingerprint"]].inputs.get("candidate") == probe["candidate"]
            and trace_results[probe["fingerprint"]].inputs.get("analysis") == probe["result"]["analysis"]
            and trace_results[probe["fingerprint"]].inputs.get("circuit_sha")
            == hashlib.sha256(probe["circuit_body"].encode()).hexdigest()
            and trace_results[probe["fingerprint"]].inputs.get("condition") == probe["result"]["condition"]
            and fixed_bias_matches(probe)
            for probe in result["probes"]
        )
        recorded = {probe["name"]: probe for probe in result["probes"]}
        model_events = [event for event in events if event.type == "model_call"]
        model_owned = bool(model_events) and result["model_calls"] == len(model_events)
        if result["mode"] == "explain":
            op = probes.get("operating_point")
            verified = (
                op is not None and op.status == "ok" and bool(op.nodes)
                and op.nodes == result.get("nodes")
                and set(result.get("expected_nodes", [])) <= op.nodes.keys()
            )
            try:
                reply = Explanation.model_validate(result.get("explanation", {}))
                text, references = render_explanation(reply, op) if op is not None else ("", [])
                candidate = Candidate.model_validate(recorded["operating_point"]["candidate"])
                dc = verify_dc(candidate, op)
                text += "\n\nDC bias checks: " + ("PASS" if dc.passed else "FAIL: " + "; ".join(dc.failures))
                verified = (verified and text == result["review_text"]
                            and references == result.get("grounding", {}).get("references")
                            and any(event.outputs.get("reply") == reply.model_dump()
                                    for event in model_events if event.outputs.get("valid")))
            except (ValueError, KeyError, TypeError):
                verified = False
        else:
            verified = False
            try:
                record = next(item for item in result["hypotheses"]
                              if item["probe"] == result.get("confirmed_probe"))
                experiment = Experiment.model_validate({key: record[key] for key in ("hypothesis", "action")})
                base = recorded["baseline"]
                repaired = recorded[record["probe"]]
                candidate = Candidate.model_validate(base["candidate"])
                body, expected_candidate, condition, change = intervention(
                    base["circuit_body"], candidate, experiment.action)
                baseline, measured = probes["baseline"], probes[record["probe"]]
                expected_bias = (None if change.get("parameter") == "I_bias"
                                 else baseline.bias_voltage_v)
                valid_intervention = (
                    verify_dc(expected_candidate, measured).passed
                    and change["kind"] != "probe" and change == result["confirmed_intervention"]
                    and repaired["circuit_body"] == body
                    and repaired["candidate"] == expected_candidate.model_dump()
                    and measured.condition == condition
                    and repaired.get("fixed_bias_v") == expected_bias
                )
                verified = valid_intervention and not verify_dc(candidate, baseline).passed
                if result.get("clipping_fault"):
                    baseline_wave = probes["baseline_transient"]
                    repaired_wave = probes[record["transient_probe"]]
                    verified = (
                        valid_intervention
                        and recorded["baseline_transient"]["circuit_body"] == base["circuit_body"]
                        and recorded["baseline_transient"]["candidate"] == candidate.model_dump()
                        and baseline_wave.condition == baseline.condition
                        and recorded["baseline_transient"].get("fixed_bias_v") == baseline.bias_voltage_v
                        and recorded[record["transient_probe"]]["circuit_body"] == body
                        and recorded[record["transient_probe"]]["candidate"] == expected_candidate.model_dump()
                        and measured.bias_voltage_v is not None
                        and recorded[record["transient_probe"]].get("fixed_bias_v") == measured.bias_voltage_v
                        and repaired_wave.condition == condition and change["kind"] != "probe"
                        and baseline_wave.transient is not None and repaired_wave.transient is not None
                        and baseline_wave.transient.clipped_or_distorted
                        and not repaired_wave.transient.clipped_or_distorted
                        and baseline_wave.transient == parse_transient(
                            Path(baseline_wave.run_dir) / "transient.tsv", baseline_wave.condition.supply_v)
                        and repaired_wave.transient == parse_transient(
                            Path(repaired_wave.run_dir) / "transient.tsv", repaired_wave.condition.supply_v)
                    )
            except (ValueError, KeyError, TypeError, StopIteration, OSError):
                verified = False
            model_owned = model_owned and bool(result.get("hypotheses")) and all(
                any(event.outputs.get("reply") == {key: record[key] for key in ("hypothesis", "action")}
                    for event in model_events if event.outputs.get("valid"))
                for record in result.get("hypotheses", [])
            )
        return [
            {"name": name, "status": "pass" if passed else "fail", "evidence": evidence}
            for name, passed, evidence in (
                ("diagnostic_grounding", grounded, "Probe bias/nodes match simulator trace events."),
                ("diagnostic_contract", bool(verified), "Independent DC/node and counterfactual checks."),
                ("model_reasoning", model_owned, "Typed hypotheses or explanation match actual model events."),
                ("diagnostic_step_cap", len(result.get("hypotheses", [])) <= result["step_limit"],
                 "Model-selected experiments obey the step cap."),
                ("simulation_budget", result["simulations_used"] <= result["budget"], "Probe execution budget."),
                ("simulation_accounting",
                 result["simulations_used"] == sum(not event.cache_hit for event in simulations)
                 and result["cache_hits"] == sum(bool(event.cache_hit) for event in simulations),
                 "Probe counters reconciled with simulation events."),
                ("trace_lifecycle", events[0].type == "task_start" and events[-1].type == "task_end",
                 "Task lifecycle recorded."),
                ("no_false_success", bool(verified) == result["verified_success"]
                 and (result["status"] != "pass" or result["verified_success"]),
                 "Reported diagnostic success agrees with independent evidence."),
            )
        ]
    events = [
        TraceEvent.model_validate_json(line)
        for line in Path(result["trace"]).read_text(encoding="utf-8").splitlines()
    ]
    simulations = [event for event in events if event.type == "simulation"]
    checks = []

    def check(name: str, passed: bool, evidence: str) -> None:
        checks.append({"name": name, "status": "pass" if passed else "fail", "evidence": evidence})

    check(
        "no_false_success",
        result["status"] != "pass" or result["verified_success"],
        f"outcome={result['status']}; independently verified success={result['verified_success']}",
    )
    check(
        "simulation_budget",
        0 <= result["simulations_used"] <= result["budget"],
        f"{result['simulations_used']} real simulations; budget={result['budget']}",
    )
    check(
        "simulation_accounting",
        result["simulations_used"] == sum(not event.cache_hit for event in simulations)
        and result["cache_hits"] == sum(bool(event.cache_hit) for event in simulations),
        "Durable task counters compared with individual simulation trace events.",
    )
    check(
        "trace_lifecycle",
        bool(events) and events[0].type == "task_start" and events[-1].type == "task_end"
        and events[-1].outputs["outcome"] == result["status"],
        "Task-start and task-end events must bracket the measured search.",
    )
    check(
        "cache_hits_do_not_execute",
        all(event.run_ms == 0 for event in simulations if event.cache_hit),
        f"{result['cache_hits']} cache hits checked for zero simulator run time.",
    )
    evidence = {
        (Candidate.model_validate(event.inputs["candidate"]).model_dump_json(), event.condition.name):
        event
        for event in simulations if event.condition is not None
        and event.inputs.get("analysis") == "ac"
    }
    grounded = True
    ranges = True
    consistent = True
    verified_success = False
    targets = Targets.model_validate(result["targets"])

    def trace_result(event: TraceEvent) -> SimulationResult:
        return SimulationResult.model_validate({
            "condition": event.condition, "analysis": event.inputs["analysis"], "status": event.status,
            **{name: event.outputs.get(name)
               for name in ("bias", "metrics", "nodes", "dc_power_w", "error", "run_dir")},
        })

    for row in result["candidates"]:
        candidate = Candidate.model_validate(row["candidate"])
        ranges &= all(math.isfinite(value) for value in candidate.model_dump().values())
        verdicts = []
        for condition, data in row["conditions"].items():
            key = (candidate.model_dump_json(), condition)
            event = evidence.get(key)
            if event is None:
                grounded = False
                verdicts.append(False)
                continue
            grounded &= (event.outputs.get("metrics") == data["metrics"]
                         and event.outputs.get("bias") == data["bias"])
            measured = SimulationResult.model_validate(trace_result(event).model_dump() | {
                "status": data["status"], "metrics": data["metrics"], "bias": data["bias"],
            })
            passed = verify(candidate, measured, targets).passed
            verdicts.append(passed)
            consistent &= passed == data["passed"]
        complete = len(row["conditions"]) == 5
        passed = complete and all(verdicts)
        verified_success |= passed
        consistent &= row["complete"] == complete and row["passed"] == passed
    check("legal_candidate_values", ranges, "Revalidated every evaluated candidate against Candidate.")
    check(
        "simulator_metric_provenance", grounded,
        "Every archived metric must exactly match a simulation trace for that candidate and condition.",
    )
    check("verification_consistency", consistent,
          "Independently recomputed DC/AC verdicts must match every condition and aggregate claim.")
    check("verified_success_consistency", result["verified_success"] == verified_success,
          "Top-level success must agree with independently reverified complete candidates.")
    dc_passed = {}
    staged = True
    for event in simulations:
        if event.condition is None:
            continue
        candidate = Candidate.model_validate(event.inputs["candidate"])
        key = (candidate.model_dump_json(), event.condition.name)
        if event.inputs["analysis"] == "op":
            dc_passed[key] = verify_dc(candidate, trace_result(event), targets).passed
        else:
            staged &= dc_passed.get(key, False)
    check("dc_before_ac", staged, "Every AC run must follow a passing DC probe for the same condition.")
    check(
        "five_condition_success",
        all(
            row["complete"] and len(row["conditions"]) == 5
            and all(data["passed"] for data in row["conditions"].values())
            for row in result["candidates"] if row["passed"]
        ),
        "Successful alternatives require all five deterministic bias/target verifications.",
    )
    if not result.get("objective_supported", True):
        checks.append({
            "name": "requested_objective_supported", "status": "missing",
            "evidence": "The task requests minimizing power; production search stops at feasibility.",
        })
    if result.get("expected_feasible", True) is False:
        check(
            "infeasible_case_not_claimed_solved", not result["verified_success"],
            "The deliberately impossible power case must not acquire a verified-success claim.",
        )
    return checks
