import math

from pydantic import ValidationError
import pytest

from agent.sim import BiasPoint, Candidate, Condition, Metrics, SimulationResult
from agent.verifier import Targets, Verification, verify, verify_dc


@pytest.fixture
def candidate() -> Candidate:
    return Candidate(
        W_in=10, L_in=1, W_load=5, L_load=1, W_tail=5, L_tail=1, I_bias=25
    )


@pytest.fixture
def targets() -> Targets:
    return Targets(gain_db=30, ugf_hz=20e6, pm_deg=60, power_w=200e-6)


@pytest.fixture
def result() -> SimulationResult:
    return SimulationResult(
        status="ok", condition=Condition(), analysis="ac",
        metrics=Metrics(gain_db=36, ugf_hz=25e6, pm_deg=75, power_w=100e-6),
        bias={
            f"m{index}": BiasPoint(
                vds=0.5, vdsat=0.2, vgs=0.6, vth=0.4, id_a=12.5e-6
            ) for index in range(1, 6)
        },
        run_dir="synthetic-test",
    )


def test_good_candidate_and_exact_output(
    candidate: Candidate, result: SimulationResult, targets: Targets
) -> None:
    verdict = verify(candidate, result, targets)
    assert verdict.model_dump() == {
        "passed": True,
        "margins": {"gain_db": 0.2, "ugf_hz": 0.25, "pm_deg": 0.25, "power_w": 0.5},
        "failures": [],
    }


@pytest.mark.parametrize("change", [{"vgs": 0.1}, {"id_a": 1e-12}])
def test_saturation_alone_does_not_make_an_off_device_pass(
    candidate: Candidate, result: SimulationResult, targets: Targets, change: dict,
) -> None:
    result.bias["m4"] = result.bias["m4"].model_copy(update=change)
    assert result.bias["m4"].vds >= result.bias["m4"].vdsat
    assert not verify(candidate, result, targets).passed
    assert not verify_dc(candidate, result, targets).passed


@pytest.mark.parametrize("name,measured,margin", [
    ("gain_db", 27, -0.1),
    ("ugf_hz", 18e6, -0.1),
    ("pm_deg", 54, -0.1),
    ("power_w", 220e-6, -0.1),
])
def test_each_target_direction(
    candidate: Candidate, result: SimulationResult, targets: Targets,
    name: str, measured: float, margin: float,
) -> None:
    assert result.metrics is not None
    result.metrics = result.metrics.model_copy(update={name: measured})
    verdict = verify(candidate, result, targets)
    assert not verdict.passed
    assert verdict.margins[name] == pytest.approx(margin)
    assert len(verdict.failures) == 1
    assert verdict.failures[0].startswith(f"{name}:")


def test_equality_passes(
    candidate: Candidate, result: SimulationResult, targets: Targets
) -> None:
    result.metrics = Metrics(**targets.model_dump())
    for point in result.bias.values():
        point.vds = point.vdsat
    verdict = verify(candidate, result, targets)
    assert verdict.passed
    assert all(margin == 0 for margin in verdict.margins.values())


@pytest.mark.parametrize("transistor", ["m1", "m2", "m3", "m4", "m5"])
def test_unsaturated_transistor_overrides_good_metrics(
    candidate: Candidate, result: SimulationResult, targets: Targets, transistor: str
) -> None:
    result.bias[transistor].vds = 0.1
    verdict = verify(candidate, result, targets)
    assert not verdict.passed
    assert all(margin > 0 for margin in verdict.margins.values())
    assert any(transistor in reason and "not saturated" in reason
               for reason in verdict.failures)


def test_reverse_pmos_bias_fails(
    candidate: Candidate, result: SimulationResult, targets: Targets
) -> None:
    result.bias["m4"].vds = -0.5
    assert not verify(candidate, result, targets).passed


@pytest.mark.parametrize("field,value", [
    ("W_in", 0.99), ("W_load", 101), ("L_tail", 0.17),
    ("L_in", 2.01), ("I_bias", 201), ("I_bias", math.nan),
])
def test_revalidate_candidate_ranges(
    candidate: Candidate, result: SimulationResult, targets: Targets,
    field: str, value: float,
) -> None:
    invalid = candidate.model_copy(update={field: value})
    verdict = verify(invalid, result, targets)
    assert not verdict.passed
    assert any(f"candidate.{field}:" in reason for reason in verdict.failures)


def test_candidate_range_endpoints(
    candidate: Candidate, result: SimulationResult, targets: Targets
) -> None:
    for widths, lengths, current in [(1, 0.18, 5), (100, 2, 200)]:
        boundary = candidate.model_copy(update={
            "W_in": widths, "W_load": widths, "W_tail": widths,
            "L_in": lengths, "L_load": lengths, "L_tail": lengths, "I_bias": current,
        })
        assert verify(boundary, result, targets).passed


@pytest.mark.parametrize("status", ["error", "timeout"])
def test_failed_simulation_overrides_good_metrics(
    candidate: Candidate, result: SimulationResult, targets: Targets, status: str
) -> None:
    failed = result.model_copy(update={"status": status, "error": "test failure"})
    verdict = verify(candidate, failed, targets)
    assert not verdict.passed
    assert f"simulation status: {status}" in verdict.failures
    assert "simulation error: test failure" in verdict.failures


def test_error_on_ok_status_fails(
    candidate: Candidate, result: SimulationResult, targets: Targets
) -> None:
    result.error = "non-convergence"
    assert not verify(candidate, result, targets).passed


def test_dc_only_cannot_pass_targets(
    candidate: Candidate, result: SimulationResult, targets: Targets
) -> None:
    result.analysis = "op"
    result.metrics = None
    verdict = verify(candidate, result, targets)
    assert not verdict.passed
    assert verdict.margins == {}
    assert "Missing performance metrics." in verdict.failures
    assert any("AC analysis" in reason for reason in verdict.failures)


def test_missing_and_unexpected_transistors(
    candidate: Candidate, result: SimulationResult, targets: Targets
) -> None:
    result.bias["m6"] = result.bias.pop("m5")
    verdict = verify(candidate, result, targets)
    assert not verdict.passed
    assert any("m5: missing" in reason for reason in verdict.failures)
    assert any("m6: unexpected" in reason for reason in verdict.failures)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_nonfinite_metrics_fail_without_invalid_json(
    candidate: Candidate, result: SimulationResult, targets: Targets, value: float
) -> None:
    assert result.metrics is not None
    result.metrics.gain_db = value
    verdict = verify(candidate, result, targets)
    assert not verdict.passed
    assert "gain_db" not in verdict.margins
    assert "finite" in verdict.failures[0]
    assert Verification.model_validate_json(verdict.model_dump_json()) == verdict


@pytest.mark.parametrize("field", ["vds", "vdsat", "vgs", "vth", "id_a"])
def test_nonfinite_bias_fails(
    candidate: Candidate, result: SimulationResult, targets: Targets, field: str
) -> None:
    result.bias["m3"] = result.bias["m3"].model_copy(update={field: math.nan})
    verdict = verify(candidate, result, targets)
    assert not verdict.passed
    assert any("m3: bias values must be finite" in reason for reason in verdict.failures)


def test_negative_vdsat_fails(
    candidate: Candidate, result: SimulationResult, targets: Targets
) -> None:
    result.bias["m1"].vdsat = -0.1
    assert not verify(candidate, result, targets).passed


@pytest.mark.parametrize("field,value", [
    ("gain_db", 0), ("ugf_hz", -1), ("power_w", math.nan), ("pm_deg", math.inf),
])
def test_invalid_targets_rejected(field: str, value: float, targets: Targets) -> None:
    with pytest.raises(ValidationError):
        Targets.model_validate({**targets.model_dump(), field: value})


def test_missing_targets_are_not_silently_defaulted(targets: Targets) -> None:
    data = targets.model_dump()
    del data["pm_deg"]
    with pytest.raises(ValidationError):
        Targets.model_validate(data)


def test_invalid_copied_targets_rejected(
    candidate: Candidate, result: SimulationResult, targets: Targets
) -> None:
    with pytest.raises(ValidationError):
        verify(candidate, result, targets.model_copy(update={"gain_db": 0}))


@pytest.mark.parametrize("name", ["ugf_hz", "power_w"])
def test_nonpositive_physical_metrics_rejected(
    candidate: Candidate, result: SimulationResult, targets: Targets, name: str
) -> None:
    assert result.metrics is not None
    result.metrics = result.metrics.model_copy(update={name: -1})
    verdict = verify(candidate, result, targets)
    assert not verdict.passed
    assert any(f"{name}: measured value must be positive" in reason
               for reason in verdict.failures)


def test_margin_overflow_cannot_pass(
    candidate: Candidate, result: SimulationResult, targets: Targets
) -> None:
    targets.gain_db = 1e-300
    assert result.metrics is not None
    result.metrics.gain_db = 1e300
    verdict = verify(candidate, result, targets)
    assert not verdict.passed
    assert "gain_db" not in verdict.margins


def test_all_failures_are_reported(
    candidate: Candidate, result: SimulationResult, targets: Targets
) -> None:
    result.metrics = Metrics(gain_db=20, ugf_hz=10e6, pm_deg=45, power_w=300e-6)
    result.bias["m5"].vds = 0.01
    verdict = verify(candidate, result, targets)
    assert not verdict.passed
    assert len(verdict.failures) == 5
    assert len(verdict.margins) == 4
