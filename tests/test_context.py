from pathlib import Path

import pytest

from agent.context import (
    LIMITS, PlannerState, ResultRow, build_context, round_line,
)
from agent.memory import StoredDesign
from agent.sim import BiasPoint, Candidate, Condition, Metrics, SimulationResult
from agent.verifier import Targets, verify


TARGETS = Targets(gain_db=30, ugf_hz=20e6, pm_deg=60, power_w=200e-6)
RAW = "RAW_NGSPICE_LINE singular matrix PRIVATE_RUN_PATH"


def candidate(i_bias: float = 25) -> Candidate:
    return Candidate(W_in=10, L_in=1, W_load=5, L_load=1, W_tail=5, L_tail=1, I_bias=i_bias)


def result(power_w: float = 45e-6, status: str = "ok") -> SimulationResult:
    return SimulationResult(
        status=status, condition=Condition(), analysis="ac",
        metrics=Metrics(gain_db=36.2, ugf_hz=27.8e6, pm_deg=83.9, power_w=power_w),
        bias={f"m{i}": BiasPoint(vds=0.5, vdsat=0.2, vgs=0.6, vth=0.4, id_a=1e-5)
              for i in range(1, 6)},
        error=None if status == "ok" else RAW, warnings=[RAW], run_dir=RAW,
    )


def row(candidate_id: str, power_w: float = 45e-6, status: str = "ok") -> ResultRow:
    item, sim = candidate(), result(power_w, status)
    return ResultRow.build(candidate_id, item, sim, verify(item, sim, TARGETS))


def example_state() -> PlannerState:
    rows = [row("r2c1"), row("r2c2", power_w=270e-6), row("r2c3", status="error")]
    design = StoredDesign(id=1, candidate=candidate(30), targets=TARGETS, results=[])
    return PlannerState(
        targets=TARGETS,
        assumptions=["Hard conditions reuse the typical targets."],
        round_lines=[round_line(1, "start from hand calculation", rows[:2])],
        best=rows[0], latest=rows, simulations_used=9, budget=40,
        lessons=["Raising I_bias raised UGF but cut phase margin."], design=design,
    )


def test_print_example_context() -> None:
    """Run with -s to read the full context the planner receives."""
    context = build_context(example_state())
    print("\n" + context.text())
    assert [name for name, _ in context.sections] == [
        "fixed", "task", "summary", "results", "retrieved",
    ]


def test_order_prompt_and_ranges_come_first() -> None:
    context = build_context(example_state())
    prompt = (Path(__file__).parents[1] / "prompts" / "planner.md").read_text(encoding="utf-8")
    assert context.system().startswith(prompt.strip())
    assert "I_bias: 5 to 200 uA" in context.system()
    text = context.user()
    order = ["TARGETS", "SUMMARY", "RESULTS (this round only)", "LESSONS", "SIMILAR PAST DESIGN"]
    assert [text.index(item) for item in order] == sorted(text.index(item) for item in order)


def test_assumptions_targets_budget_and_best_are_shown() -> None:
    text = build_context(example_state()).user()
    assert "- ASSUMPTION: Hard conditions reuse the typical targets." in text
    assert "ugf_hz >= 20 MHz" in text and "power_w <= 200 uW" in text
    assert "9 of 40 simulations used, 31 left" in text
    assert "Best so far: r2c1" in text and "margin +20.7%" in text
    assert "round 1:" in text


def test_latest_results_are_compact_and_numeric() -> None:
    text = build_context(example_state()).user()
    assert "r2c1 | typical | 10 1 5 1 5 1 25 | 36.2 27.8 83.9 45.0 | pass" in text
    assert "power_w short by 35.0%" in text
    assert "r2c3 | typical | 10 1 5 1 5 1 25 | - - - - | FAIL: simulation error" in text


def test_raw_simulator_text_never_included() -> None:
    text = build_context(example_state()).text()
    assert "RAW_NGSPICE_LINE" not in text and "secret" not in text
    assert "measured 0.00027" not in text


def test_retrieved_limits_three_lessons_one_design() -> None:
    state = example_state()
    state.lessons = [f"lesson {i}" for i in range(5)]
    text = build_context(state).user()
    assert "lesson 2" in text and "lesson 3" not in text
    assert text.count("SIMILAR PAST DESIGN") == 1


def test_every_section_respects_its_limit() -> None:
    state = example_state()
    state.assumptions = ["x" * 500] * 10
    state.round_lines = [f"round {i}: " + "y" * 200 for i in range(100)]
    state.latest = [row(f"r9c{i}") for i in range(200)]
    state.lessons = ["z" * 2000] * 3
    context = build_context(state)
    for name, text in context.sections:
        assert len(text) <= LIMITS[name], name


def test_overflow_keeps_newest_rounds_and_reports_omissions() -> None:
    state = example_state()
    state.round_lines = [f"round {i}: tried something" for i in range(300)]
    summary = dict(build_context(state).sections)["summary"]
    assert "round 299:" in summary and "round 0:" not in summary
    assert "earlier rounds omitted)" in summary


def test_no_improvement_note() -> None:
    state = example_state()
    state.no_improvement_rounds = 2
    assert "try a clearly different region" in build_context(state).user()


def test_empty_state() -> None:
    text = build_context(PlannerState(targets=TARGETS)).user()
    assert "Best so far: none yet." in text
    assert "(no results yet)" in text
    assert "(none)" in text


def test_round_line_summarizes_failures() -> None:
    line = round_line(4, "more current", [row("a"), row("b", power_w=270e-6)])
    assert line.startswith("round 4: tried 'more current'; 2 condition results, 1 passed")
    assert "power_w x1" in line


@pytest.mark.parametrize("status", ["error", "timeout"])
def test_failed_rows_drop_metrics(status: str) -> None:
    failed = row("x", status=status)
    assert failed.metrics is None
    assert failed.issues[0] == f"simulation {status}"
