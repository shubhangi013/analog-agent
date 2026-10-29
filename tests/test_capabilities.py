import copy
import hashlib
import json
from pathlib import Path

import pytest

from agent.diagnostics import DEFAULT_CANDIDATE
from agent.lessons import HOLDOUT, accepts_validation
from agent.memory import Memory
from agent.predictor import predict, prediction_error
from agent.sim import Metrics
from agent.spec import parse_request
from agent.tradeoffs import Option, describe_options, objective_key
from agent.verifier import Targets


TARGETS = Targets(gain_db=30, ugf_hz=20e6, pm_deg=60, power_w=200e-6)


def validation_evidence() -> dict:
    score = {"integrity": True, "verified_success": True, "budget": 40,
             "first_pass": 20, "simulations_used": 36, "trace": "unit-test-only.jsonl"}
    return {
        "heldout_sha": hashlib.sha256(HOLDOUT.read_bytes()).hexdigest(),
        "candidate_sha": hashlib.sha256(DEFAULT_CANDIDATE.model_dump_json().encode()).hexdigest(),
        "provenance": "unit-test-provenance",
        "pairs": [{"case_id": case["id"], "baseline": copy.deepcopy(score),
                   "proposed": copy.deepcopy(score)}
                  for case in json.loads(HOLDOUT.read_text())["cases"]],
    }


def test_engineer_spec_parses_objective_units_and_explicit_defaults() -> None:
    request = parse_request(
        "minimize power with gain >= 35 dB, bandwidth >= 25 MHz and power <= 150 uW", TARGETS)
    assert request.objective == "minimize_power"
    assert request.targets == Targets(gain_db=35, ugf_hz=25e6, pm_deg=60, power_w=150e-6)
    assert len(request.assumptions) == 1 and "pm_deg" in request.assumptions[0]


def test_engineer_spec_parses_compact_quantity_first_wording() -> None:
    goal = "at least 30dB gain, 20MHz bandwidth, 60deg phase margin, 200uW max"
    request = parse_request(goal, TARGETS)
    assert request.goal == goal
    assert request.targets == TARGETS
    assert request.assumptions == []


@pytest.mark.parametrize("power", ["200 uW", "0.2 mW", "0.0002 W"])
def test_decimal_unit_conversion_preserves_the_independent_target_contract(power: str) -> None:
    request = parse_request(f"power <= {power}", TARGETS)
    assert request.targets.power_w == TARGETS.power_w


@pytest.mark.parametrize("spec", [
    "gain > 35 dB", "power < 150 uW", "gain >= 30 dB and gain >= 35 dB",
    "noise <= 10 nV", "gain >= 30 dB at 1.8 V", "build a cascode",
    "maximize gain and minimize power", "minimize area",
])
def test_unsupported_or_ambiguous_spec_is_not_silently_discarded(spec: str) -> None:
    with pytest.raises(ValueError):
        parse_request(spec, TARGETS)


def test_alternatives_and_deltas_are_computed_from_measured_metrics() -> None:
    options = [
        Option(candidate_id="low_power", candidate=DEFAULT_CANDIDATE, conditions=[],
               worst=Metrics(gain_db=34, ugf_hz=25e6, pm_deg=70, power_w=50e-6)),
        Option(candidate_id="more_gain", candidate=DEFAULT_CANDIDATE, conditions=[],
               worst=Metrics(gain_db=38, ugf_hz=30e6, pm_deg=65, power_w=80e-6)),
    ]
    assert max(options, key=lambda option: objective_key(option, "minimize_power")) == options[0]
    assert max(options, key=lambda option: objective_key(option, "maximize_gain")) == options[1]
    text = describe_options(options, "minimize_power")
    assert "power +30.000 uW, gain +4.000 dB, bandwidth +5.000 MHz" in text
    assert "not proof of a global optimum" in text
    assert "no alternatives are invented" in describe_options(options[:1], "feasibility")
    assert "No fully verified" in describe_options([], "feasibility")


def test_prediction_requires_observations_and_never_mutates_measurements() -> None:
    candidate = DEFAULT_CANDIDATE
    measured = Metrics(gain_db=36, ugf_hz=25e6, pm_deg=75, power_w=100e-6)
    observations = [(candidate, measured)] * 3
    before = measured.model_dump()
    assert predict(candidate, observations[:2]) is None
    estimate = predict(candidate, observations)
    assert estimate is not None
    assert estimate.gain_db == pytest.approx(measured.gain_db)
    changed = estimate.model_copy(update={"gain_db": 39})
    assert prediction_error(changed, measured, TARGETS)["gain_db"] == pytest.approx(0.1)
    assert measured.model_dump() == before


@pytest.mark.parametrize("change", ["success", "cost", "integrity", "budget", "digest", "malformed"])
def test_lesson_gate_rejects_regressions_and_malformed_evidence(change: str) -> None:
    evidence = validation_evidence()
    assert accepts_validation(evidence)
    proposed = evidence["pairs"][0]["proposed"]
    if change == "success":
        proposed.update(verified_success=False, first_pass=None)
    elif change == "cost":
        proposed["first_pass"] += 1
    elif change == "integrity":
        proposed["integrity"] = False
    elif change == "budget":
        proposed["budget"] += 1
    elif change == "digest":
        evidence["heldout_sha"] = "changed"
    else:
        del proposed["verified_success"]
    assert not accepts_validation(evidence)


def test_mutual_failure_cannot_hide_higher_execution_cost() -> None:
    evidence = validation_evidence()
    pair = evidence["pairs"][0]
    for arm in ("baseline", "proposed"):
        pair[arm].update(verified_success=False, first_pass=None, simulations_used=30)
    assert accepts_validation(evidence)
    pair["proposed"]["simulations_used"] = 31
    assert not accepts_validation(evidence)


def test_only_accepted_candidate_bound_lessons_can_be_retrieved(tmp_path: Path) -> None:
    memory = Memory(tmp_path / "memory.sqlite3")
    lesson = memory.store_lesson("test", "Unit-test-only candidate prior.",
                                 candidate=DEFAULT_CANDIDATE, provenance="unit-test-provenance")
    assert memory.lesson_records() == []
    evidence = validation_evidence()
    assert memory.promote_lesson(lesson, evidence)
    assert memory.lesson_records()[0]["id"] == lesson
    evidence["candidate_sha"] = "different-candidate"
    assert not memory.promote_lesson(lesson, evidence)
    assert memory.lesson_records() == []
