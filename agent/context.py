"""Bounded planner context, rebuilt from typed state before every model call.

Only our own numbers and short reasons go in. Raw ngspice output, simulator
error text, warnings and run paths are never copied into the context.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agent.memory import CachedSimulator, StoredDesign
from agent.sim import Candidate, Metrics, SimulationResult
from agent.smoke import ROOT
from agent.tools import candidate_ranges
from agent.verifier import Targets, Verification, verify
from agent.spec import Objective


# Character limits per section, in assembly order.
LIMITS = {"fixed": 2500, "task": 800, "summary": 1800, "results": 2600, "retrieved": 1200}
UNITS = {"W": "um", "L": "um", "I": "uA"}
TARGET_TEXT = {
    "gain_db": (">=", "dB", 1), "ugf_hz": (">=", "MHz", 1e6),
    "pm_deg": (">=", "deg", 1), "power_w": ("<=", "uW", 1e-6),
}


class ResultRow(BaseModel):
    """One simulated candidate at one condition, reduced to numbers and short reasons."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    candidate_id: str
    candidate: Candidate
    condition: str = "typical"
    status: Literal["ok", "error", "timeout"]
    metrics: Metrics | None = None
    margins: dict[str, float] = Field(default_factory=dict)
    passed: bool = False
    issues: list[str] = Field(default_factory=list)

    @classmethod
    def build(
        cls, candidate_id: str, candidate: Candidate,
        result: SimulationResult, verdict: Verification,
    ) -> "ResultRow":
        # Drop reasons that would carry simulator text; keep our own short reasons.
        short = {name: margin for name, margin in verdict.margins.items() if margin < 0}
        issues = [] if result.status == "ok" else [f"simulation {result.status}"]
        issues += [
            reason[:100] for reason in verdict.failures
            if not reason.startswith(("simulation error", "simulation status"))
            and not any(reason.startswith(f"{name}:") for name in short)
        ]
        issues += [f"{name} short by {-margin:.1%}" for name, margin in short.items()]
        return cls(
            candidate_id=candidate_id, candidate=candidate,
            condition=result.condition.name, status=result.status,
            metrics=result.metrics if result.status == "ok" else None,
            margins=verdict.margins, passed=verdict.passed, issues=issues[:3],
        )

    def worst_margin(self) -> float:
        return min(self.margins.values()) if len(self.margins) == 4 else -1e9


class PlannerState(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    targets: Targets
    objective: Objective = "feasibility"
    assumptions: list[str] = Field(default_factory=list)
    round_lines: list[str] = Field(default_factory=list)
    best: ResultRow | None = None
    latest: list[ResultRow] = Field(default_factory=list)
    simulations_used: int = Field(default=0, ge=0)
    budget: int = Field(default=40, ge=1)
    no_improvement_rounds: int = Field(default=0, ge=0)
    all_conditions_passed: bool = False
    lessons: list[str] = Field(default_factory=list)
    dc_failure_patterns: list[str] = Field(default_factory=list)
    design: StoredDesign | None = None


class Context(BaseModel):
    sections: list[tuple[str, str]]

    def system(self) -> str:
        return self.sections[0][1]

    def user(self) -> str:
        return "\n\n".join(text for _, text in self.sections[1:])

    def text(self) -> str:
        return "\n\n".join(f"=== {name} ===\n{text}" for name, text in self.sections)


def fit(text: str, limit: int) -> str:
    marker = "\n[truncated]"
    return text if len(text) <= limit else text[: limit - len(marker)] + marker


def fit_lines(header: list[str], lines: list[str], footer: list[str], limit: int,
              *, keep: Literal["first", "last"], what: str) -> str:
    """Keep as many whole lines as fit, and say how many were left out."""
    chosen = list(lines)
    while chosen:
        omitted = len(lines) - len(chosen)
        note = [f"({omitted} {what} omitted)"] if omitted else []
        text = "\n".join(header + (note + chosen if keep == "last" else chosen + note) + footer)
        if len(text) <= limit:
            return text
        chosen = chosen[1:] if keep == "last" else chosen[:-1]
    note = [f"({len(lines)} {what} omitted)"] if lines else []
    return fit("\n".join(header + note + footer), limit)


def format_candidate(candidate: Candidate) -> str:
    return " ".join(f"{name}={value:g}" for name, value in candidate.model_dump().items())


def format_target(name: str, target: float) -> str:
    comparison, unit, scale = TARGET_TEXT[name]
    return f"{name} {comparison} {target / scale:g} {unit}"


def format_metric(name: str, value: float) -> str:
    _, unit, scale = TARGET_TEXT[name]
    return f"{value / scale:.3g} {unit}"


def fixed_section() -> str:
    prompt = (ROOT / "prompts" / "planner.md").read_text(encoding="utf-8").strip()
    ranges = [
        f"{name}: {low:g} to {high:g} {UNITS[name[0]]}"
        for name, (low, high) in candidate_ranges().items()
    ]
    return fit("\n".join([prompt, "", "RANGES", *ranges]), LIMITS["fixed"])


def task_section(state: PlannerState) -> str:
    lines = ["TARGETS", f"Objective: {state.objective}; optimize only among verified feasible designs.",
             *(format_target(n, t) for n, t in state.targets.model_dump().items())]
    if state.assumptions:
        lines += ["ASSUMPTIONS (defaults chosen by the program, not given in the request):"]
        lines += [f"- ASSUMPTION: {item[:200]}" for item in state.assumptions]
    return fit("\n".join(lines), LIMITS["task"])


def summary_section(state: PlannerState) -> str:
    left = max(0, state.budget - state.simulations_used)
    header = [
        "SUMMARY",
        f"Budget: {state.simulations_used} of {state.budget} simulations used, {left} left.",
        f"All-condition success confirmed by verifier: {state.all_conditions_passed}.",
    ]
    best = state.best
    if best is None or best.metrics is None:
        header.append("Best so far: none yet.")
    else:
        header.append(
            f"Best so far: {best.candidate_id} at {best.condition} "
            f"({'passes' if best.passed else 'fails'}): {format_candidate(best.candidate)}"
        )
        for name, target in state.targets.model_dump().items():
            margin = best.margins.get(name)
            margin_text = "n/a" if margin is None else f"{margin:+.1%}"
            header.append(
                f"  {name}: {format_metric(name, getattr(best.metrics, name))} "
                f"(target {format_target(name, target).split(' ', 1)[1]}, margin {margin_text})"
            )
    footer = []
    if state.no_improvement_rounds >= 2:
        footer.append(
            f"NOTE: {state.no_improvement_rounds} rounds in a row showed no improvement; "
            "try a clearly different region."
        )
    lines = [f"- {line[:240]}" for line in state.round_lines]
    header.append("Earlier rounds:" if lines else "Earlier rounds: none yet.")
    return fit_lines(header, lines, footer, LIMITS["summary"], keep="last", what="earlier rounds")


def result_line(row: ResultRow) -> str:
    values = " ".join(f"{value:g}" for value in row.candidate.model_dump().values())
    if row.metrics is None:
        numbers = "- - - -"
    else:
        m = row.metrics
        numbers = f"{m.gain_db:.1f} {m.ugf_hz / 1e6:.1f} {m.pm_deg:.1f} {m.power_w * 1e6:.1f}"
    verdict = "pass" if row.passed else "FAIL: " + "; ".join(row.issues or ["unknown"])
    return f"{row.candidate_id} | {row.condition} | {values} | {numbers} | {verdict}"


def results_section(state: PlannerState) -> str:
    targets = " | ".join(format_target(n, t) for n, t in state.targets.model_dump().items())
    header = [
        "RESULTS (this round only)",
        f"targets: {targets}",
        "id | condition | W_in L_in W_load L_load W_tail L_tail I_bias | "
        "gain_dB ugf_MHz pm_deg power_uW | verdict",
    ]
    if not state.latest:
        return "\n".join(header + ["(no results yet)"])
    lines = [result_line(row) for row in state.latest]
    return fit_lines(header, lines, [], LIMITS["results"], keep="first", what="rows")


def retrieved_section(state: PlannerState) -> str:
    lines = ["LESSONS"]
    lines += [f"- {lesson[:300]}" for lesson in state.lessons[:3]] or ["(none)"]
    if state.dc_failure_patterns:
        lines += ["MEASURED CROSS-TASK DC FAILURE PATTERNS"]
        lines += [f"- {pattern[:240]}" for pattern in state.dc_failure_patterns[:3]]
    if state.design is not None:
        design = state.design
        targets = ", ".join(format_target(n, t) for n, t in design.targets.model_dump().items())
        lines += [
            "SIMILAR PAST DESIGN (passed all conditions)",
            f"targets: {targets}",
            f"candidate: {format_candidate(design.candidate)}",
        ]
    return fit("\n".join(lines), LIMITS["retrieved"])


def build_context(state: PlannerState) -> Context:
    return Context(sections=[
        ("fixed", fixed_section()),
        ("task", task_section(state)),
        ("summary", summary_section(state)),
        ("results", results_section(state)),
        ("retrieved", retrieved_section(state)),
    ])


def round_line(index: int, hypothesis: str, rows: list[ResultRow]) -> str:
    """One short line describing a finished round, for later SUMMARY sections."""
    passed = sum(row.passed for row in rows)
    failing: dict[str, int] = {}
    for row in rows:
        for issue in row.issues:
            key = issue.split(":", 1)[0].split(" short by", 1)[0]
            failing[key] = failing.get(key, 0) + 1
    best = max(rows, key=lambda row: (row.passed, row.worst_margin()), default=None)
    best_text = (
        f"best {best.candidate_id} worst margin {best.worst_margin():+.1%}"
        if best is not None and best.margins else "no usable result"
    )
    fails = ", ".join(f"{key} x{count}" for key, count in sorted(failing.items())) or "none"
    return (
        f"round {index}: tried '{hypothesis[:100]}'; {len(rows)} condition results, "
        f"{passed} passed; {best_text}; failures: {fails}"
    )


def demo_state(simulator: CachedSimulator) -> PlannerState:
    """Real round-0 state: the hand-calculated start plus one wide, high-current check."""
    targets = Targets(gain_db=30, ugf_hz=20e6, pm_deg=60, power_w=200e-6)
    round0 = [
        ("r0c1", Candidate(W_in=10, L_in=1, W_load=5, L_load=1, W_tail=5, L_tail=1, I_bias=25)),
        ("r0c2", Candidate(W_in=100, L_in=1, W_load=100, L_load=1, W_tail=100, L_tail=1,
                           I_bias=150)),
    ]
    rows = []
    for candidate_id, candidate in round0:
        result = simulator.simulate(candidate).result
        rows.append(ResultRow.build(candidate_id, candidate, result, verify(candidate, result, targets)))
    best = max(rows, key=lambda row: (row.passed, row.worst_margin()))
    return PlannerState(
        targets=targets,
        assumptions=["Hard conditions use the same targets as typical (not stated in the request)."],
        best=best, latest=rows, simulations_used=simulator.simulations_used,
    )


def main() -> int:
    from uuid import uuid4

    from agent.memory import Memory
    from agent.tracer import Tracer

    simulator = CachedSimulator(Memory(), Tracer("context_demo", uuid4().hex))
    context = build_context(demo_state(simulator))
    print(context.text())
    print()
    for name, text in context.sections:
        print(f"{name}: {len(text)} of {LIMITS[name]} characters")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
