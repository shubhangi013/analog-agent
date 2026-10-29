"""Deterministic verification of one candidate at one condition."""

import json
import math

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent.sim import Candidate, SimulationResult, simulate


TRANSISTORS = ("m1", "m2", "m3", "m4", "m5")


class Targets(BaseModel):
    """Gain, UGF and phase margin are minima; power is a maximum."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    gain_db: float = Field(gt=0)
    ugf_hz: float = Field(gt=0)
    pm_deg: float = Field(gt=0)
    power_w: float = Field(gt=0)


class Verification(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    passed: bool
    margins: dict[str, float]
    failures: list[str]


def verify_dc(
    candidate: Candidate, result: SimulationResult, targets: Targets | None = None,
) -> Verification:
    """Conservative on-state, saturation and power gate before any AC work."""
    failures: list[str] = []
    margins: dict[str, float] = {}
    try:
        Candidate.model_validate(candidate.model_dump())
    except ValidationError as error:
        failures.extend(
            f"candidate.{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}"
            for issue in error.errors()
        )

    if result.status != "ok":
        failures.append(f"simulation status: {result.status}")
    if result.error is not None:
        failures.append(f"simulation error: {result.error}")
    for name in TRANSISTORS:
        point = result.bias.get(name)
        if point is None:
            failures.append(f"{name}: missing transistor bias point.")
            continue
        if not all(math.isfinite(value) for value in point.model_dump().values()):
            failures.append(f"{name}: bias values must be finite.")
            continue
        if point.vgs <= point.vth or point.id_a <= 1e-9:
            failures.append(
                f"{name}: device is off or below the strong-inversion gate "
                f"(vgs={point.vgs:g} V, vth={point.vth:g} V, id={point.id_a:g} A)."
            )
        if point.vdsat < 0:
            failures.append(f"{name}: vdsat must be a nonnegative magnitude.")
        elif point.vds < point.vdsat:
            failures.append(
                f"{name}: vds={point.vds:g} V < vdsat={point.vdsat:g} V "
                "(not saturated)."
            )
    for name in sorted(result.bias.keys() - set(TRANSISTORS)):
        failures.append(f"{name}: unexpected transistor for the fixed 5T structure.")
    if targets is not None:
        power = result.dc_power_w
        if power is None and result.metrics is not None:
            power = result.metrics.power_w
        if power is None or not math.isfinite(power) or power <= 0:
            failures.append("power_w: missing or invalid DC supply power.")
        elif power > targets.power_w:
            failures.append(f"power_w: DC power {power:g} exceeds {targets.power_w:g} W.")
    return Verification(passed=not failures, margins=margins, failures=failures)


def verify(
    candidate: Candidate, result: SimulationResult, targets: Targets
) -> Verification:
    """Check targets and hard constraints without calling a model or simulator."""
    targets = Targets.model_validate(targets.model_dump())
    dc = verify_dc(candidate, result)
    failures, margins = list(dc.failures), {}
    if result.analysis != "ac":
        failures.append("AC analysis is required to verify all performance targets.")

    if result.metrics is None:
        failures.append("Missing performance metrics.")
    else:
        for name, target in targets.model_dump().items():
            measured = getattr(result.metrics, name)
            if not math.isfinite(measured):
                failures.append(f"{name}: measured value must be finite.")
                continue
            if name in ("ugf_hz", "power_w") and measured <= 0:
                failures.append(f"{name}: measured value must be positive.")
            margin = (
                (target - measured) / target if name == "power_w"
                else (measured - target) / target
            )
            if not math.isfinite(margin):
                failures.append(f"{name}: normalized margin is not finite.")
                continue
            margins[name] = margin
            if margin < 0:
                comparison = "<=" if name == "power_w" else ">="
                failures.append(
                    f"{name}: measured {measured:g}, target {comparison} {target:g} "
                    f"(margin {margin:.3%})."
                )

    return Verification(passed=not failures, margins=margins, failures=failures)


def main() -> int:
    targets = Targets(gain_db=30, ugf_hz=20e6, pm_deg=60, power_w=200e-6)
    good = Candidate(
        W_in=10, L_in=1, W_load=5, L_load=1, W_tail=5, L_tail=1, I_bias=25
    )
    bad = Candidate(
        W_in=100, L_in=1, W_load=100, L_load=1, W_tail=100, L_tail=1, I_bias=150
    )
    verdicts = []
    for label, candidate in (("good", good), ("bad", bad)):
        result = simulate(candidate)
        verdict = verify(candidate, result, targets)
        verdicts.append(verdict)
        print(f"{label} candidate: {json.dumps(verdict.model_dump(), sort_keys=True)}")
        print(f"Simulation log: {result.run_dir}")
    if not verdicts[0].passed or verdicts[1].passed or not any(
        reason.startswith("power_w:") for reason in verdicts[1].failures
    ):
        print("VERIFIER FAIL: expected a passing candidate and a power-target failure.")
        return 1
    print("VERIFIER PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
