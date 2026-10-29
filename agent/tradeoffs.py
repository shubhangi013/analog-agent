"""Simulator-only option selection and quantitative comparative explanations."""

from pydantic import BaseModel

from agent.sim import Candidate, Metrics, SimulationResult
from agent.spec import Objective
from agent.verifier import Targets, verify


class Option(BaseModel):
    candidate_id: str
    candidate: Candidate
    worst: Metrics
    conditions: list[SimulationResult]


def objective_key(option: Option, objective: Objective) -> float:
    if objective == "minimize_power":
        return -option.worst.power_w
    if objective == "maximize_gain":
        return option.worst.gain_db
    if objective == "maximize_bandwidth":
        return option.worst.ugf_hz
    return min(option.worst.gain_db, option.worst.pm_deg)


def measured_options(
    candidates: dict[str, Candidate], results: dict[str, SimulationResult],
    targets: Targets, condition_names: list[str], objective: Objective,
    excluded: set[str] | None = None,
) -> list[Option]:
    options = []
    for cid, candidate in candidates.items():
        if cid in (excluded or set()):
            continue
        conditions = [results.get(f"{cid}|{name}") for name in condition_names]
        if any(result is None or not verify(candidate, result, targets).passed
               for result in conditions):
            continue
        complete = [result for result in conditions if result is not None]
        metrics = [result.metrics for result in complete if result.metrics is not None]
        worst = Metrics(
            gain_db=min(m.gain_db for m in metrics), ugf_hz=min(m.ugf_hz for m in metrics),
            pm_deg=min(m.pm_deg for m in metrics), power_w=max(m.power_w for m in metrics),
        )
        options.append(Option(candidate_id=cid, candidate=candidate, worst=worst,
                              conditions=complete))
    return sorted(options, key=lambda option: objective_key(option, objective), reverse=True)


def describe_options(options: list[Option], objective: Objective) -> str:
    lines = [
        f"Objective: {objective}. Best observed designs, not proof of a global optimum.",
        "All numbers below are worst-case simulator results across the required conditions.",
        "id | gain dB | UGF MHz | PM deg | power uW",
    ]
    shown = options[:3]
    for option in shown:
        m = option.worst
        lines.append(f"{option.candidate_id} | {m.gain_db:.3f} | {m.ugf_hz / 1e6:.3f} | "
                     f"{m.pm_deg:.3f} | {m.power_w * 1e6:.3f}")
    if not shown:
        lines.append("No fully verified options are available.")
    elif len(shown) < 2:
        lines.append("Only one fully verified option is available; no alternatives are invented.")
    for option in shown[1:]:
        a, b = shown[0].worst, option.worst
        lines.append(
            f"{option.candidate_id} versus {shown[0].candidate_id}: "
            f"power {(b.power_w - a.power_w) * 1e6:+.3f} uW, "
            f"gain {b.gain_db - a.gain_db:+.3f} dB, "
            f"bandwidth {(b.ugf_hz - a.ugf_hz) / 1e6:+.3f} MHz, "
            f"phase margin {b.pm_deg - a.pm_deg:+.3f} deg."
        )
    return "\n".join(lines)
