import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent import memory as memory_module
from agent.planner import ChatResponse, LLMError
from agent.sim import BiasPoint, Metrics, SimulationResult
from eval.checks import heuristic_checks
from eval.judge import DIMENSIONS, build_evidence, judge_result
from eval.report import write_report
from eval.run_eval import EvalCase, EvalSuite, SUITE, main, random_candidates, run_case


@pytest.fixture
def case() -> EvalCase:
    return EvalCase(
        id="easy", mode="optimize", difficulty="easy", goal="Meet explicit targets.",
        targets={"gain_db": 30, "ugf_hz": 20e6, "pm_deg": 60, "power_w": 200e-6},
    )


@pytest.fixture
def fake_spice(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(memory_module, "find_ngspice", lambda: "fake")
    monkeypatch.setattr(memory_module, "ngspice_version", lambda executable: "fake version")

    def simulate(candidate, condition, **kwargs):
        bias = {f"m{index}": BiasPoint(
            vds=0.5, vdsat=0.2, vgs=0.7, vth=0.4, id_a=12.5e-6,
        ) for index in range(1, 6)}
        body = kwargs.get("netlist") or kwargs.get("circuit") or ""
        if re.search(r"(?im)^\s*M4\s+\S+\s+vdd\s", body):
            bias["m4"] = bias["m4"].model_copy(update={"vgs": 0, "id_a": 1e-12})
        load = re.search(r"(?im)^Rload\s+out\s+0\s+([0-9.eE+-]+)", body)
        overloaded = load is not None and float(load[1]) < 1e6
        if overloaded:
            bias["m2"] = bias["m2"].model_copy(update={"vds": 0.01})
        run_dir = "fake_simulator_unit_test_only"
        transient = None
        if kwargs["analysis"] == "tran":
            import math
            from uuid import uuid4
            from agent.sim import parse_transient
            path = tmp_path / uuid4().hex
            path.mkdir()
            rows = []
            for index in range(5001):
                time_s = index * 1e-9
                sine = math.sin(2 * math.pi * 1e6 * time_s)
                out = 0.02 + 1e-5 * sine if overloaded else 0.9 + 0.05 * sine
                rows.append(f"{time_s:.15g} {0.9+0.001*sine:.15g} {0.9-0.001*sine:.15g} {out:.15g}")
            (path / "transient.tsv").write_text("\n".join(rows) + "\n")
            transient = parse_transient(path / "transient.tsv", condition.supply_v)
            run_dir = str(path)
        return SimulationResult(
            status="ok", condition=condition, analysis=kwargs["analysis"], dc_power_w=100e-6,
            metrics=Metrics(gain_db=36, ugf_hz=30e6, pm_deg=75, power_w=100e-6)
            if kwargs["analysis"] == "ac" else None,
            bias=bias, nodes={"out": 1.0, "vdd": condition.supply_v, "inp": 0.9, "inn": 0.9,
                             "mirror": 1.0, "tail": 0.3, "tail_d": 0.3, "bias": 0.6},
            run_dir=run_dir, transient=transient,
            bias_voltage_v=0.6, tail_current_a=25e-6,
        )

    monkeypatch.setattr(memory_module, "simulate", simulate)
    from agent.reasoning import Reasoner
    import agent.diagnostics as diagnostics

    def reasoning_chat(messages):
        facts = json.loads(messages[1]["content"].split("\nPrevious response rejected:", 1)[0])
        if "measured_facts" in facts:
            return ChatResponse(json.dumps({
                "overview": "M1 and M2 form the differential input pair; M3 and M4 form the "
                            "mirror load; M5 supplies tail current through the Vsense connection.",
                "devices": {
                    name: "Measured operating point: " + "; ".join(
                        "{{" + key + "}}" for key in facts["measured_facts"]
                        if key.startswith("bias." + name + "."))
                    for name in facts["device_keys"]},
                "nodes": {name: "Connected node at {{nodes." + name + "}}."
                          for name in facts["node_keys"]},
                "limitations": "DC data does not establish AC or transient performance.",
            }))
        connections = facts["connections"]
        action = (
            {"tool": "rewire_gate", "device": "m4", "node": connections["m3"][1]}
            if connections["m4"][1] != connections["m3"][1] else
            {"tool": "dc_probe", "device": "m4"}
        )
        return ChatResponse(json.dumps({"hypothesis": "Inspect the mirror gate bias.", "action": action}))

    monkeypatch.setattr(diagnostics, "Reasoner",
                        lambda tracer, chat=None: Reasoner(tracer, chat or reasoning_chat))


def test_random_search_is_seeded_distinct_and_not_local_only() -> None:
    first = random_candidates(17, [])
    assert first == random_candidates(17, [])
    assert first != random_candidates(18, [])
    assert len({candidate.model_dump_json() for candidate in first}) == 6
    assert not set(candidate.model_dump_json() for candidate in first) & {
        candidate.model_dump_json() for candidate in random_candidates(17, first)
    }
    assert any(candidate.W_in > 30 for candidate in first)


def test_yaml_spec_contract_checks_source_values_and_discloses_null_defaults() -> None:
    from agent.spec import parse_request
    from agent.verifier import Targets
    from eval.spec_list import EngineerSpec, intake_contract
    spec = EngineerSpec(
        id="O01", category="objective", objective="minimize_power",
        targets={"gain_db_min": 30, "ugf_hz_min": 20e6, "pm_deg_min": 60},
        spec_text="Minimize power with gain >= 30 dB, UGF >= 20 MHz, phase margin >= 60 deg.",
        expected="feasible", why="An estimate, not an electrical oracle.",
    )
    defaults = Targets(gain_db=30, ugf_hz=20e6, pm_deg=60, power_w=200e-6)
    request = parse_request(spec.spec_text, defaults)
    assert all(check["status"] == "pass" for check in intake_contract(spec, request))
    changed = request.model_copy(update={"targets": defaults.model_copy(update={"gain_db": 29})})
    assert any(check["status"] == "fail" for check in intake_contract(spec, changed))
    undisclosed = request.model_copy(update={"assumptions": []})
    assert next(check for check in intake_contract(spec, undisclosed)
                if check["name"] == "default_power_w")["status"] == "fail"


def test_yaml_batch_preserves_clarifications_and_does_not_treat_expectation_as_ground_truth(
    tmp_path: Path, fake_spice: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace
    import yaml
    from eval import spec_list
    monkeypatch.setattr(spec_list.LLMConfig, "from_env",
                        lambda: SimpleNamespace(model="unit-test-model"))
    monkeypatch.setattr(spec_list, "ngspice_version", lambda executable: "fake version")
    monkeypatch.setattr(spec_list, "find_ngspice", lambda: "fake")
    monkeypatch.setattr(spec_list, "Reasoner", lambda tracer: SimpleNamespace(calls=0))
    inputs = [
        {"id": "E01", "category": "easy", "objective": "feasibility",
         "targets": {"gain_db_min": 30, "ugf_hz_min": 20e6, "pm_deg_min": 60, "power_w_max": 200e-6},
         "spec_text": "Gain >= 30 dB, UGF >= 20 MHz, phase margin >= 60 deg, power <= 200 uW.",
         "expected": "infeasible", "why": "Deliberately wrong estimate: measurements must win."},
        {"id": "A06", "category": "parser", "objective": "feasibility", "targets": {},
         "spec_text": "Minimize noise.", "expected": "clarify", "why": "Unsupported objective."},
    ]
    source = tmp_path / "source.yaml"
    source.write_text(yaml.safe_dump({"specs": inputs}), encoding="utf-8")
    results = spec_list.run_specs(
        source, tmp_path / "batch", strategy="random", judge=False, case_workers=2)
    assert len(results) == 2 and results[0]["verified_success"]
    assert all(result["heuristic_status"] == "pass" for result in results)
    assert results[1]["status"] == "clarification_required" and results[1]["simulations_used"] == 0
    assert (tmp_path / "batch" / "source_specs.yaml").read_bytes() == source.read_bytes()
    assert "Null targets" in (tmp_path / "batch" / "spec_report.md").read_text()
    before = (tmp_path / "batch" / "E01_random_s0" / "result.json").read_bytes()

    def unexpected_work(*args, **kwargs):
        raise AssertionError("A completed case must not repeat intake, search or judging.")

    monkeypatch.setattr(spec_list, "run_case", unexpected_work)
    monkeypatch.setattr(spec_list, "route_request", unexpected_work)
    resumed = spec_list.run_specs(
        source, tmp_path / "batch", strategy="random", judge=False, resume=True, case_workers=2)
    assert resumed == results
    assert (tmp_path / "batch" / "E01_random_s0" / "result.json").read_bytes() == before
    with pytest.raises(ValueError, match="changed budget"):
        spec_list.run_specs(
            source, tmp_path / "batch", strategy="random", judge=False, resume=True, budget=41)


def test_interrupted_eval_arm_resumes_its_checkpoint_without_repeating_simulations(
    tmp_path: Path, case: EvalCase, fake_spice: None,
) -> None:
    first = run_case(case, "random", 0, 40, tmp_path)
    resumed = run_case(case, "random", 0, 40, tmp_path, resume=True)
    assert resumed["verified_success"] and resumed["simulations_used"] == first["simulations_used"]
    assert all(check["status"] == "pass" for check in heuristic_checks(resumed))


def test_suite_has_real_difficulty_ladder_and_missing_capability_contracts() -> None:
    suite = EvalSuite.model_validate_json(SUITE.read_text(encoding="utf-8"))
    assert {case.mode for case in suite.cases} == {"optimize", "debug", "explain"}
    assert {case.difficulty for case in suite.cases} == {"easy", "medium", "hard"}
    assert any(not case.expected_feasible for case in suite.cases)
    with pytest.raises(ValidationError, match="unique"):
        EvalSuite(cases=[suite.cases[0], suite.cases[0]])
    with pytest.raises(ValidationError, match="targets"):
        EvalCase(id="bad", mode="optimize", difficulty="easy", goal="No targets")


def test_random_run_uses_real_scheduler_and_independent_verification(
    tmp_path: Path, case: EvalCase, fake_spice: None,
) -> None:
    result = run_case(case, "random", 7, 30, tmp_path)
    assert result["status"] == "pass"
    assert result["verified_success"]
    assert result["simulations_used"] == 28
    assert result["cache_hits"] == 0
    assert result["model_calls"] == 0
    assert not any("Hand" in item["text"] or "overdrive" in item["text"]
                   for item in result["reasoning_artifacts"])
    assert 5 <= result["simulations_to_first_pass"] <= 30
    assert len(result["pareto_options"]) == 2
    assert all(check["status"] == "pass" for check in heuristic_checks(result))
    assert "automatic_eval" in result["engineer_verdict"]
    assert not list(tmp_path.rglob("sized.cir"))
    assert result["progress"][-1]["best_complete_worst_margin"] >= 0


def test_budget_does_not_count_partial_corners_as_success(
    tmp_path: Path, case: EvalCase, fake_spice: None,
) -> None:
    result = run_case(case, "random", 0, 4, tmp_path)
    assert result["status"] == "budget_exhausted"
    assert not result["verified_success"]
    assert result["simulations_used"] == 4
    assert result["simulations_to_first_pass"] is None
    assert result["pareto_options"] == []
    assert all(point["best_complete_worst_margin"] is None for point in result["progress"])
    assert all(check["status"] == "pass" for check in heuristic_checks(result))
    result["verified_success"] = True
    checks = {check["name"]: check["status"] for check in heuristic_checks(result)}
    assert checks["verified_success_consistency"] == "fail"


def test_cold_cache_isolation_between_baseline_seeds(
    tmp_path: Path, case: EvalCase, fake_spice: None,
) -> None:
    one = run_case(case, "random", 0, 4, tmp_path)
    two = run_case(case, "random", 1, 4, tmp_path)
    assert one["simulations_used"] == two["simulations_used"] == 4
    assert one["cache_hits"] == two["cache_hits"] == 0


def test_hard_checks_reject_false_pass_and_edited_numbers(
    tmp_path: Path, case: EvalCase, fake_spice: None,
) -> None:
    result = run_case(case, "random", 0, 12, tmp_path)
    result["status"] = "pass"
    result["candidates"][0]["conditions"]["typical"]["metrics"]["gain_db"] = 9999
    checks = {item["name"]: item["status"] for item in heuristic_checks(result)}
    assert checks["no_false_success"] == "fail"
    assert checks["simulator_metric_provenance"] == "fail"
    assert checks["trace_lifecycle"] == "fail"


def test_unconfirmed_fault_is_not_a_passing_diagnosis(tmp_path: Path, fake_spice: None) -> None:
    case = EvalCase(
        id="fault", mode="debug", difficulty="medium", goal="Find a fault.",
        fixture="M4 gate tied to vdd", expected=["Name the cause and cite probes."],
    )
    result = run_case(case, "random", 0, 40, tmp_path)
    assert result["status"] == "unresolved"
    assert not result["verified_success"]
    assert list(tmp_path.rglob("memory.sqlite3"))
    assert any(check["name"] == "diagnostic_contract" and check["status"] == "fail"
               for check in heuristic_checks(result))


@pytest.mark.parametrize("mode", ["debug", "explain"])
def test_diagnostic_modes_are_grounded_in_probe_traces(
    tmp_path: Path, fake_spice: None, mode: str,
) -> None:
    case = EvalCase(id="diagnostic", mode=mode, difficulty="easy", goal="Use DC evidence.",
                    fault="mirror_gate" if mode == "debug" else None)
    result = run_case(case, "random", 0, 6, tmp_path)
    assert result["verified_success"], result["review_text"]
    assert all(check["status"] == "pass" for check in heuristic_checks(result))
    result["probes"][0]["result"]["bias"]["m4"]["vgs"] = 999
    assert heuristic_checks(result)[0]["status"] == "fail"


def test_validated_prior_is_reverified_first_with_cold_cache(
    tmp_path: Path, case: EvalCase, fake_spice: None,
) -> None:
    from agent.diagnostics import DEFAULT_CANDIDATE
    result = run_case(case, "random", 0, 40, tmp_path, warm_candidate=DEFAULT_CANDIDATE)
    assert result["simulations_to_first_pass"] == 10
    assert result["cache_hits"] == 0
    assert len(result["pareto_options"]) == 3
    assert all(check["status"] == "pass" for check in heuristic_checks(result))


def test_archived_verdict_cannot_hide_a_tampered_off_device(
    tmp_path: Path, case: EvalCase, fake_spice: None,
) -> None:
    result = run_case(case, "random", 0, 40, tmp_path)
    result["candidates"][0]["conditions"]["typical"]["bias"]["m4"]["vgs"] = 0
    checks = {check["name"]: check["status"] for check in heuristic_checks(result)}
    assert checks["simulator_metric_provenance"] == "fail"
    assert checks["verification_consistency"] == "fail"


def test_learning_sequence_isolates_caches_and_reports_validation_cost(
    tmp_path: Path, case: EvalCase, fake_spice: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eval import learning

    def validation_stub(memory, lesson_id, directory, *, budget, strategy):
        directory.mkdir()
        evidence = {"accepted": True, "pairs": [{"baseline": {"simulations_used": 20},
                                               "proposed": {"simulations_used": 10}}]}
        (directory / "validation.json").write_text(json.dumps(evidence))
        return evidence

    monkeypatch.setattr(learning, "validate_lesson", validation_stub)
    directory = tmp_path / "sequence"
    rows = learning.run_sequence(directory, [case, case.model_copy(update={"id": "next"})])
    assert [row["prior_used"] for row in rows] == [False, True]
    assert rows[1]["with"]["first_pass"] == 10
    assert sum(row["validation_simulations"] for row in rows) == 60
    report = (directory / "report.md").read_text()
    assert "additional compute" in report and "not model fine-tuning" in report
    assert all(not json.loads(path.read_text())["cache_hits"]
               for path in directory.rglob("result.json"))
    assert (directory / "learning.svg").is_file()


def test_model_diagnosis_adapts_to_latest_probe_and_logs_each_hypothesis(
    tmp_path: Path, fake_spice: None,
) -> None:
    from agent.diagnostics import run_diagnostic
    proposals = iter([
        {"tool": "dc_probe", "device": "m4"},
        {"tool": "rewire_gate", "device": "m4", "node": "mirror"},
    ])
    contexts = []

    def chat(messages):
        contexts.append(json.loads(messages[1]["content"]))
        return ChatResponse(json.dumps({"hypothesis": "Test the measured mirror fault.",
                                        "action": next(proposals)}))

    result = run_diagnostic("debug", directory=tmp_path / "adaptive", planted_fault=True, chat=chat)
    assert result["verified_success"]
    assert result["model_calls"] == 2 and result["cache_hits"] == 1
    assert len(result["hypotheses"]) == 2
    assert contexts[1]["probes"][-1]["name"] == "experiment_1"
    assert contexts[1]["previous_hypotheses"][0]["dc_passed"] is False
    assert any(item["name"] == "Vsense" for item in contexts[1]["components"])
    assert "run_dir" not in json.dumps(contexts)
    assert all(check["status"] == "pass" for check in heuristic_checks(result))


def test_diagnostic_model_cannot_override_gates_or_exceed_the_step_cap(
    tmp_path: Path, fake_spice: None,
) -> None:
    from agent.diagnostics import run_diagnostic
    chat = lambda messages: ChatResponse(json.dumps({
        "hypothesis": "The mirror needs another bias measurement.",
        "action": {"tool": "dc_probe", "device": "m4"},
    }))
    result = run_diagnostic("debug", directory=tmp_path / "capped", planted_fault=True,
                            chat=chat, step_limit=2)
    assert result["status"] == "unresolved" and not result["verified_success"]
    assert result["model_calls"] == 2 and len(result["hypotheses"]) == 2
    assert result["simulations_used"] == 1 and result["cache_hits"] == 2


def test_illegal_model_intervention_is_retried_but_never_simulated(
    tmp_path: Path, fake_spice: None,
) -> None:
    from agent.diagnostics import run_diagnostic
    chat = lambda messages: ChatResponse(json.dumps({
        "hypothesis": "Try a stronger bias source.",
        "action": {"tool": "change_parameter", "parameter": "I_bias", "value": 500},
    }))
    result = run_diagnostic("debug", directory=tmp_path / "illegal", planted_fault=True, chat=chat)
    assert result["status"] == "error" and not result["verified_success"]
    assert result["model_calls"] == 2 and result["simulations_used"] == 1
    assert result["hypotheses"] == []


@pytest.mark.parametrize("invented", ["Gain is 123 dB.", "The node is one volt.", "{{unknown.value}}"])
def test_explanation_rejects_invented_digits_words_and_unknown_facts(
    tmp_path: Path, fake_spice: None, invented: str,
) -> None:
    from agent.diagnostics import run_diagnostic
    from agent.explain import Explanation, render_explanation
    result = run_diagnostic("explain", directory=tmp_path / "explanation")
    assert result["verified_success"]
    op = SimulationResult.model_validate(result["probes"][0]["result"])
    reply = Explanation.model_validate(result["explanation"]).model_copy(update={"overview": invented})
    with pytest.raises(ValueError):
        render_explanation(reply, op)
    result["review_text"] += "\n" + invented
    assert any(check["name"] == "diagnostic_contract" and check["status"] == "fail"
               for check in heuristic_checks(result))


def test_model_strategy_accepts_a_prior_and_reverifies_it_instead_of_reusing_a_verdict(
    tmp_path: Path, fake_spice: None, case: EvalCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent.diagnostics import DEFAULT_CANDIDATE
    from eval.run_eval import RandomPlanner, Planner
    monkeypatch.setattr(Planner, "from_env", lambda tracer: RandomPlanner(tracer, 0))
    result = run_case(case, "agent", 0, 40, tmp_path, warm_candidate=DEFAULT_CANDIDATE)
    assert result["strategy"] == "agent"
    assert result["simulations_to_first_pass"] == 10 and result["cache_hits"] == 0
    assert all(check["status"] == "pass" for check in heuristic_checks(result))


def test_clipping_diagnosis_requires_real_waveform_evidence_before_and_after(
    tmp_path: Path, fake_spice: None,
) -> None:
    from agent.diagnostics import run_diagnostic
    chat = lambda messages: ChatResponse(json.dumps({
        "hypothesis": "The resistive load is pulling the output into the lower rail.",
        "action": {"tool": "change_parameter", "parameter": "load_ohm", "value": 10000000},
    }))
    result = run_diagnostic("debug", directory=tmp_path / "clipping",
                            clipping_fault=True, chat=chat)
    assert result["verified_success"] and result["simulations_used"] == 4
    assert result["hypotheses"][0]["transient_passed"]
    assert Path(result["waveform_report"]).is_file()
    assert all(check["status"] == "pass" for check in heuristic_checks(result))
    result["probes"][-1]["result"]["transient"]["rail_fraction"] = 1
    assert heuristic_checks(result)[0]["status"] == "fail"


def test_feasible_result_does_not_claim_to_minimize_power(
    tmp_path: Path, case: EvalCase, fake_spice: None,
) -> None:
    case = case.model_copy(update={"objective": "minimize_power"})
    result = run_case(case, "random", 0, 30, tmp_path)
    assert result["verified_success"]
    checks = {item["name"]: item["status"] for item in heuristic_checks(result)}
    assert result["objective_supported"]
    assert "not proof of a global optimum" in result["review_text"]
    assert all(status == "pass" for status in checks.values())


def test_offline_cli_persists_report_and_refuses_overwrite(
    tmp_path: Path, case: EvalCase, fake_spice: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import eval.run_eval as runner
    monkeypatch.setattr(runner, "find_ngspice", lambda: "fake")
    monkeypatch.setattr(runner, "ngspice_version", lambda executable: "fake version")
    suite = tmp_path / "suite.json"
    suite.write_text(EvalSuite(cases=[case]).model_dump_json(), encoding="utf-8")
    out = tmp_path / "results"
    args = [
        "--suite", str(suite), "--out", str(out), "--strategy", "random",
        "--seed", "0", "--budget", "4", "--judge", "off",
    ]
    assert main(args) == 0
    before = (out / "results.json").read_bytes()
    assert main(args) == 2
    assert (out / "results.json").read_bytes() == before
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["judge_model"] is None
    assert manifest["source_sha256"]
    assert (out / "progress.svg").exists()
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "not measured" in report
    assert "not a human verdict" in report


def judgment_reply(score: int = 2, reference: str = "outcome") -> str:
    return json.dumps({
        "dimensions": {
            name: {"score": score, "rationale": "Supported only partially.", "evidence": [reference]}
            for name in DIMENSIONS
        },
        "summary": "Only assesses observed engineering communication.",
    })


def test_judge_is_evidence_bounded_and_cannot_override_hard_checks(
    tmp_path: Path, case: EvalCase, fake_spice: None,
) -> None:
    result = run_case(case, "random", 0, 4, tmp_path)
    result["error"] = "RAW_SPICE_MUST_NOT_LEAK"
    result["heuristic_checks"] = [{"name": "deliberate_failure", "status": "fail", "evidence": "test"}]
    sent = []

    def chat(messages):
        sent.extend(messages)
        return ChatResponse(judgment_reply(4), {"total_tokens": 23})

    judgment = judge_result(result, chat, "judge-test", tmp_path / "judge")
    assert judgment["status"] == "scored"
    assert judgment["mean_score"] == 4
    assert judgment["attempts"][0]["usage"]["total_tokens"] == 23
    assert not result["verified_success"]
    assert result["heuristic_checks"][0]["status"] == "fail"
    assert "RAW_SPICE_MUST_NOT_LEAK" not in json.dumps(sent)
    assert not any("run_dir" in json.dumps(value) for value in build_evidence(result).values())
    assert (tmp_path / "judge" / "judge_input.json").exists()


@pytest.mark.parametrize("invalid", [
    judgment_reply(5),
    judgment_reply(2, "invented_evidence"),
    '{"dimensions":{},"summary":"Omitted the rubric."}',
    "not JSON",
])
def test_judge_retries_invalid_scores_and_references(
    tmp_path: Path, invalid: str,
) -> None:
    replies = iter([invalid, judgment_reply()])
    result = {"mode": "optimize", "strategy": "agent"}
    judgment = judge_result(
        result, lambda messages: ChatResponse(next(replies)), "judge-test", tmp_path,
    )
    assert judgment["status"] == "scored"
    assert len(judgment["attempts"]) == 2
    assert not judgment["attempts"][0]["valid"]


def test_failed_judge_never_becomes_a_zero_or_passing_score(tmp_path: Path) -> None:
    def unavailable(messages):
        raise LLMError("endpoint unavailable")

    judgment = judge_result(
        {"mode": "optimize", "strategy": "agent"}, unavailable, "judge-test", tmp_path,
    )
    assert judgment["status"] == "error"
    assert "mean_score" not in judgment
    assert len(judgment["attempts"]) == 2


def test_report_keeps_missing_and_failed_runs_visible(tmp_path: Path) -> None:
    (tmp_path / "results.json").write_text(json.dumps([{
        "case_id": "fault", "strategy": "agent", "seed": 0, "mode": "debug",
        "status": "missing", "verified_success": False,
        "reason": "No debug implementation.", "expected": ["Find M4 fault."],
        "heuristic_status": "missing", "llm_judge": {"status": "not_run"},
    }]), encoding="utf-8")
    report = write_report(tmp_path).read_text(encoding="utf-8")
    assert "No debug implementation." in report
    assert "No fully verified feasible alternatives observed" in report
    assert "No candidate has complete simulator metrics" in (
        tmp_path / "progress.svg"
    ).read_text(encoding="utf-8")
