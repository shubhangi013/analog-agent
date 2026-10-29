from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any
import json

import pytest

from agent import orchestrator
from agent.memory import CachedSimulation, CachedSimulator, Memory
from agent.orchestrator import Orchestrator, load_targets, main, size_start
from agent.planner import ChatResponse, ConfigError, Planner
from agent.sim import BiasPoint, Candidate, Condition, Metrics, SimulationResult
from agent.tools import make_candidates, retrieve_designs
from agent.tracer import Tracer
from agent.verifier import Targets


TARGETS = Targets(gain_db=30, ugf_hz=20e6, pm_deg=60, power_w=200e-6)
PASS = Metrics(gain_db=36, ugf_hz=25e6, pm_deg=75, power_w=100e-6)
FAIL = Metrics(gain_db=10, ugf_hz=25e6, pm_deg=75, power_w=100e-6)
BASE = Candidate(W_in=20, L_in=0.5, W_load=10, L_load=0.5, W_tail=10, L_tail=0.5, I_bias=25)
FAKE_FINGERPRINT = "0" * 64


class FakeSimulator(CachedSimulator):
    """Replaces only the ngspice call; the real budget reservation and job wrappers still run."""

    def provenance(self) -> str:
        return "unit-test-simulator-provenance"

    def __init__(
        self, memory: Memory, tracer: Tracer, metrics: Metrics, fail_attempts: tuple[int, ...],
    ) -> None:
        super().__init__(memory, tracer)
        self.metrics = metrics
        self.fail_attempts = set(fail_attempts)
        self.attempts = 0
        self._lock = Lock()
        self.calls: list[tuple[str, str, str]] = []

    def _simulate(  # type: ignore[override]
        self, candidate: Candidate, condition: Condition, analysis: str,
        timeout_s: float, before_run: Any = None, circuit: str | None = None,
    ) -> CachedSimulation:
        if before_run is not None:
            before_run()
        with self._lock:
            self.attempts += 1
            attempt = self.attempts
        if attempt in self.fail_attempts:
            raise RuntimeError("simulator killed")
        self.calls.append((candidate.model_dump_json(), condition.name, analysis))
        bias = {
            f"m{index}": BiasPoint(vds=0.5, vdsat=0.2, vgs=0.6, vth=0.4, id_a=12.5e-6)
            for index in range(1, 6)
        }
        result = SimulationResult(
            status="ok", condition=condition, analysis=analysis,
            metrics=self.metrics if analysis == "ac" else None, dc_power_w=self.metrics.power_w,
            bias=bias, run_dir="fake",
        )
        return CachedSimulation(
            result=result, fingerprint=FAKE_FINGERPRINT, cache_hit=False, queue_wait_ms=0, run_ms=0,
        )


class ScriptedChat:
    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls = 0

    def __call__(self, messages: list[dict[str, str]]) -> ChatResponse:
        self.calls += 1
        return ChatResponse(self.replies.pop(0), {"total_tokens": 100})


@dataclass
class Harness:
    orch: Orchestrator
    memory: Memory
    tracer: Tracer
    sim: FakeSimulator
    chat: ScriptedChat
    exports: list[tuple[int, Path]]


def engineer(verdict: str) -> Any:
    """Scripted engineer: the verdict at the review prompt, keep at pauses."""
    return lambda prompt: verdict if "approve" in prompt else "keep"


def fake_exporter(calls: list[tuple[int, Path]]) -> Any:
    def export(design_id: int, out_dir: Path) -> dict[str, Any]:
        calls.append((design_id, out_dir))
        return {"folder": str(out_dir), "round_trip": {"passed": True}}
    return export


def build(
    tmp_path: Path, *, run_id: str = "run", budget: int = 40, yes: bool = True,
    read: Any = None, metrics: Metrics = PASS, fail_attempts: tuple[int, ...] = (),
    replies: tuple[str, ...] = (), targets: Targets = TARGETS, circuit_sha: str = "",
    circuit: str | None = None,
) -> Harness:
    memory = Memory(tmp_path / "memory.sqlite3")
    tracer = Tracer("easy", run_id, tmp_path)
    sim = FakeSimulator(memory, tracer, metrics, fail_attempts)
    chat = ScriptedChat(*replies)
    exports: list[tuple[int, Path]] = []
    orch = Orchestrator(
        "easy", run_id, targets=targets, memory=memory, tracer=tracer, simulator=sim,
        planner=Planner(chat, tracer, "gpt-test"), budget=budget, yes=yes,
        read=read or engineer("approve"), circuit=circuit, circuit_sha=circuit_sha,
        exporter=fake_exporter(exports), export_root=tmp_path / "exports",
    )
    return Harness(orch, memory, tracer, sim, chat, exports)


def reply(seed: int) -> str:
    candidates = make_candidates(BASE, 6, 0.9, seed=seed)
    return json.dumps({
        "hypothesis": "try a different region",
        "candidates": [item.model_dump() for item in candidates],
        "stop": False,
    })


def events(tracer: Tracer) -> list[dict[str, Any]]:
    return [json.loads(line) for line in tracer.path.read_text(encoding="utf-8").splitlines()]


def test_start_is_sized_from_targets() -> None:
    start, assumptions = size_start(TARGETS)
    assert start.I_bias > 0
    assert len(assumptions) == 4


@pytest.mark.parametrize("failed_condition", ["typical", "hot_low_supply"])
def test_failed_dc_never_gets_ac_or_later_conditions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_condition: str,
) -> None:
    harness = build(tmp_path)
    original = harness.sim._simulate

    def fail_dc(*args, **kwargs):
        answer = original(*args, **kwargs)
        if answer.result.analysis == "op" and answer.result.condition.name == failed_condition:
            answer.result.bias["m4"].vgs = 0
        return answer

    monkeypatch.setattr(harness.sim, "_simulate", fail_dc)
    state = harness.orch._load_or_start()
    assert not harness.orch._simulate(state)
    assert not any(condition == failed_condition and analysis == "ac"
                   for _, condition, analysis in harness.sim.calls)
    assert not any(condition.startswith("cold") or condition == "hot_high_supply"
                   for _, condition, _ in harness.sim.calls)


def test_failed_corner_survivors_are_backfilled_with_deferred_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build(tmp_path, budget=32)
    state = harness.orch._load_or_start()
    failed = {
        state.candidates["r0c1"].model_dump_json(),
        state.candidates["r0c2"].model_dump_json(),
    }
    original = harness.sim._simulate

    def fail_first_batch(*args, **kwargs):
        answer = original(*args, **kwargs)
        candidate, condition, analysis = args[:3]
        if (
            candidate.model_dump_json() in failed
            and condition.name == "hot_low_supply"
            and analysis == "ac"
        ):
            answer.result.metrics = FAIL
        return answer

    monkeypatch.setattr(harness.sim, "_simulate", fail_first_batch)

    assert not harness.orch._simulate(state)
    assert harness.orch._winner(state) == "r0c3"
    selections = [
        event["outputs"]["selected"] for event in events(harness.tracer)
        if event["type"] == "tool_call"
        and event.get("inputs", {}).get("tool") == "select_survivors"
    ]
    assert selections == [["r0c1", "r0c2"], ["r0c3", "r0c4"]]


def test_resume_rejects_changed_simulation_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build(tmp_path)
    harness.orch._load_or_start()
    monkeypatch.setattr(harness.sim, "provenance", lambda: "changed")
    result = harness.orch.run()
    assert result.outcome == "error" and "changed" in result.message
    assert harness.sim.calls == []


def test_optimizer_tracks_objective_improvement_not_only_target_margin(tmp_path: Path) -> None:
    harness = build(tmp_path)
    harness.orch.objective = "minimize_power"
    state = harness.orch._load_or_start()
    harness.orch._simulate(state)
    cid = harness.orch._winner(state)
    assert cid is not None
    for key, result in state.results.items():
        if key.startswith(cid + "|") and result.metrics is not None:
            result.metrics = result.metrics.model_copy(update={"gain_db": 31, "power_w": 70e-6})
    state.execution.update(round=1, round_ids=[cid], no_improvement=2)
    harness.orch._evaluate(state)
    assert state.execution["no_improvement"] == 0
    assert state.best_candidate_id == cid
    assert harness.orch._planner_state(state).best.candidate == state.candidates[cid]


def test_unknown_task_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown task"):
        load_targets("no-such-task")


def test_passing_design_is_stored_after_approval(tmp_path: Path) -> None:
    harness = build(tmp_path, yes=True)
    result = harness.orch.run()
    assert result.outcome == "pass"
    assert result.design_id is not None
    assert [design.id for design in harness.memory.designs()] == [result.design_id]
    assert harness.orch.verdict == "approve"
    assert harness.chat.calls == 0
    assert len(harness.sim.calls) == 6 * 2 + 3 * 4 * 2
    assert events(harness.tracer)[-1]["outputs"]["outcome"] == "pass"
    assert harness.exports == [(result.design_id, tmp_path / "exports" / "run")]


def test_approved_netlist_design_keeps_body_and_provenance(tmp_path: Path) -> None:
    body = "* engineer circuit body\n"
    harness = build(tmp_path, circuit=body, circuit_sha="c" * 64)
    result = harness.orch.run()
    design = harness.memory.design(result.design_id)
    assert design.circuit_body == body
    assert design.circuit_sha == "c" * 64
    assert design.provenance == {
        "task_id": "easy", "run_id": "run",
        "simulations_used": result.simulations_used, "cache_hits": result.cache_hits,
        "simulation_provenance": harness.sim.provenance(),
    }
    assert retrieve_designs(harness.memory, TARGETS, circuit_sha="c" * 64,
                            provenance=harness.sim.provenance())
    assert not retrieve_designs(harness.memory, TARGETS, circuit_sha="c" * 64,
                                provenance="different-model")


def test_export_failure_keeps_the_approved_design(tmp_path: Path) -> None:
    harness = build(tmp_path)

    def broken(design_id: int, out_dir: Path) -> dict[str, Any]:
        raise OSError("disk full")

    harness.orch.exporter = broken
    result = harness.orch.run()
    assert result.outcome == "delivery_failed"
    assert result.design_id is not None
    assert harness.orch.export_summary == {
        "folder": str(tmp_path / "exports" / "run"), "error": "disk full",
    }


def test_rejected_pass_is_not_stored(tmp_path: Path) -> None:
    harness = build(tmp_path, yes=False, read=engineer("reject"))
    result = harness.orch.run()
    assert result.outcome == "pass_rejected"
    assert result.design_id is None
    assert harness.memory.designs() == []


def test_budget_exhaustion_stops_before_corners(tmp_path: Path) -> None:
    harness = build(tmp_path, budget=5)
    result = harness.orch.run()
    assert result.outcome == "budget_exhausted"
    assert result.simulations_used == 5
    assert len(harness.sim.calls) == 5
    assert all(condition == "typical" and analysis == "op"
               for _, condition, analysis in harness.sim.calls)


def test_flat_rounds_stop_the_run(tmp_path: Path) -> None:
    harness = build(tmp_path, budget=60, metrics=FAIL,
                    replies=tuple(reply(seed) for seed in (101, 102, 103)))
    result = harness.orch.run()
    assert result.outcome == "stalled"
    assert harness.chat.calls == 3
    assert result.simulations_used == 6 * 2 * 4


def test_resume_does_not_rerun_saved_jobs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orchestrator, "WORKERS", 1)
    first = build(tmp_path, run_id="crash", fail_attempts=(4,))
    assert first.orch.run().outcome == "error"
    saved = first.sim.calls[:3]
    assert all(condition == "typical" and analysis == "op" for _, condition, analysis in saved)

    second = build(tmp_path, run_id="crash")
    assert second.orch.run().outcome == "pass"
    rerun = set(second.sim.calls)
    assert not rerun & set(saved)


def test_resume_rejects_changed_targets(tmp_path: Path) -> None:
    build(tmp_path, run_id="mismatch").orch.run()
    changed = Targets(gain_db=31, ugf_hz=20e6, pm_deg=60, power_w=200e-6)
    result = build(tmp_path, run_id="mismatch", targets=changed).orch.run()
    assert result.outcome == "error"
    assert "different targets" in result.message


def test_model_intake_is_checkpointed_and_not_reinterpreted_on_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent.intake as intake
    import agent.reasoning as reasoning
    from agent.spec import parse_request
    goal = "Please provide sufficient amplification."
    request = parse_request("gain >= 31 dB", TARGETS).model_copy(update={"goal": goal})
    calls = []

    def interpret(source, defaults, reasoner):
        calls.append(source)
        return request

    monkeypatch.setattr(intake, "interpret_request", interpret)
    monkeypatch.setattr(reasoning, "Reasoner", lambda tracer: object())
    first = build(tmp_path)
    first.orch.goal_to_interpret = goal
    assert first.orch.run().outcome == "pass"
    checkpoint = first.memory.load_checkpoint("easy", "run")
    assert checkpoint.state.execution["resolved_request"]["targets"]["gain_db"] == 31
    assert events(first.tracer)[0]["type"] == "task_start"
    assert events(first.tracer)[-1]["type"] == "task_end"
    resumed = build(tmp_path)
    resumed.orch.goal_to_interpret = goal
    assert resumed.orch.run().outcome == "pass"
    assert calls == [goal] and resumed.sim.calls == []
    assert resumed.orch.targets.gain_db == 31


def test_ambiguous_intake_stops_before_any_simulation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent.intake as intake
    import agent.reasoning as reasoning

    def ambiguous(*args):
        raise intake.ClarificationNeeded("What minimum gain should be used?")

    monkeypatch.setattr(intake, "interpret_request", ambiguous)
    monkeypatch.setattr(reasoning, "Reasoner", lambda tracer: object())
    harness = build(tmp_path)
    harness.orch.goal_to_interpret = "Give it good gain."
    outcome = harness.orch.run()
    assert outcome.outcome == "clarification_required" and outcome.simulations_used == 0
    assert "minimum gain" in outcome.message
    assert harness.sim.calls == [] and harness.exports == []


def test_engineer_can_decline_model_parsed_targets_before_simulation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent.intake as intake
    import agent.reasoning as reasoning
    from agent.spec import parse_request
    monkeypatch.setattr(intake, "interpret_request",
                        lambda goal, defaults, reasoner: parse_request("gain >= 31 dB", defaults))
    monkeypatch.setattr(reasoning, "Reasoner", lambda tracer: object())
    harness = build(tmp_path, yes=False, read=lambda prompt: "no")
    harness.orch.goal_to_interpret = "I need more amplification."
    outcome = harness.orch.run()
    assert outcome.outcome == "stopped_by_engineer" and outcome.simulations_used == 0
    assert harness.sim.calls == [] and harness.exports == []


def test_unknown_task_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--task", "no-such-task"]) == 2
    assert "Unknown task" in capsys.readouterr().err


def test_missing_planner_configuration_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    def must_not_run(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("storage or tracing was created before the configuration check")

    monkeypatch.setattr(orchestrator, "Memory", must_not_run)
    monkeypatch.setattr(orchestrator, "Tracer", must_not_run)

    def missing() -> None:
        raise ConfigError("AZURE_FOUNDRY_ENDPOINT is not set")

    monkeypatch.setattr(orchestrator.LLMConfig, "from_env", staticmethod(missing))
    assert main(["--task", "easy", "--run-id", "cfg"]) == 2
    assert "NOT CONFIGURED" in capsys.readouterr().err


def test_failed_job_keeps_finished_siblings(tmp_path: Path) -> None:
    first = build(tmp_path, run_id="partial", fail_attempts=(2,))
    assert first.orch.run().outcome == "error"
    finished_typical = [call for call in first.sim.calls if call[1] == "typical"]
    assert len(finished_typical) == 5

    second = build(tmp_path, run_id="partial")
    assert second.orch.run().outcome == "pass"
    rerun_typical = [call for call in second.sim.calls
                     if call[1] == "typical" and call[2] == "op"]
    assert len(rerun_typical) == 1
    assert rerun_typical[0] not in finished_typical


def fake_result(condition: Condition, metrics: Metrics) -> SimulationResult:
    bias = {
        f"m{index}": BiasPoint(vds=0.5, vdsat=0.2, vgs=0.6, vth=0.4, id_a=12.5e-6)
        for index in range(1, 6)
    }
    return SimulationResult(
        status="ok", condition=condition, analysis="ac", metrics=metrics, bias=bias, run_dir="fake",
    )


def fake_dc_result(condition: Condition, *, failed_device: str | None = None) -> SimulationResult:
    bias = {
        f"m{index}": BiasPoint(vds=0.5, vdsat=0.2, vgs=0.6, vth=0.4, id_a=12.5e-6)
        for index in range(1, 6)
    }
    if failed_device is not None:
        bias[failed_device].vds = 0.1
    return SimulationResult(
        status="ok", condition=condition, analysis="op", dc_power_w=PASS.power_w,
        bias=bias, run_dir="fake",
    )


def pauses(*answers: str, asked: list[str] | None = None) -> Any:
    """Answers pause prompts in order, then keeps; approves the final review."""
    queue = list(answers)

    def read(prompt: str) -> str:
        if "approve" in prompt:
            return "approve"
        if asked is not None:
            asked.append(prompt)
        return queue.pop(0) if queue else "keep"

    return read


def test_pauses_show_progress_and_record_keep(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    harness = build(tmp_path, run_id="keep", yes=False, read=pauses())
    result = harness.orch.run()
    out = capsys.readouterr().out
    assert result.outcome == "pass"
    assert "  r0c1 typical: PASS" in out
    assert "Pause: candidate r0c1 passes typical" in out
    decisions = harness.memory.load_checkpoint("easy", "keep").state.execution["decisions"]
    assert len(decisions) == 6
    assert set(decisions.values()) == {"keep"}


def test_drop_excludes_candidate_from_corners_and_winner(tmp_path: Path) -> None:
    harness = build(tmp_path, run_id="drop", yes=False, read=pauses("drop"))
    assert harness.orch.run().outcome == "pass"
    state = harness.memory.load_checkpoint("easy", "drop").state
    assert state.execution["decisions"]["r0c1"] == "drop"
    dropped = state.candidates["r0c1"].model_dump_json()
    assert dropped not in {call for call, condition, _ in harness.sim.calls if condition != "typical"}
    assert harness.memory.designs()[0].candidate == state.candidates["r0c2"]


def test_stop_at_pause_ends_before_corners(tmp_path: Path) -> None:
    harness = build(tmp_path, run_id="stop", yes=False, read=pauses("stop"))
    result = harness.orch.run()
    assert result.outcome == "stopped_by_engineer"
    assert len(harness.sim.calls) == 12
    assert all(condition == "typical" for _, condition, _ in harness.sim.calls)
    assert harness.memory.designs() == []


def test_resume_does_not_ask_again_for_decided_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orchestrator, "WORKERS", 1)
    first_asked: list[str] = []
    first = build(
        tmp_path, run_id="pausecrash", yes=False, read=pauses(asked=first_asked),
        fail_attempts=(14,),
    )
    assert first.orch.run().outcome == "error"
    assert len(first_asked) == 6

    second_asked: list[str] = []
    second = build(tmp_path, run_id="pausecrash", yes=False, read=pauses(asked=second_asked))
    assert second.orch.run().outcome == "pass"
    assert second_asked == []


def test_resume_refuses_a_different_netlist(tmp_path: Path) -> None:
    assert build(tmp_path, run_id="netmix", circuit_sha="a" * 64).orch.run().outcome == "pass"
    result = build(tmp_path, run_id="netmix", circuit_sha="b" * 64).orch.run()
    assert result.outcome == "error"
    assert "different netlist" in result.message


def test_design_library_is_keyed_by_netlist(tmp_path: Path) -> None:
    first = build(tmp_path, run_id="lib1", circuit_sha="a" * 64)
    assert first.orch.run().outcome == "pass"
    second = build(tmp_path, run_id="lib2", circuit_sha="b" * 64)
    assert second.orch.run().outcome == "pass"
    assert len(second.memory.designs()) == 2
    assert len(retrieve_designs(second.memory, TARGETS, circuit_sha="a" * 64)) == 1
    assert len(retrieve_designs(second.memory, TARGETS, circuit_sha="c" * 64)) == 0


def test_pin_overrides_best_as_next_base(tmp_path: Path) -> None:
    orch = build(tmp_path, run_id="pin").orch
    state = orch._load_or_start()
    state.results["r0c1|typical"] = fake_result(orchestrator.TYPICAL, PASS)
    state.results["r0c3|typical"] = fake_result(orchestrator.TYPICAL, FAIL)
    state.execution["decisions"] = {"r0c3": "pin"}
    planner_state = orch._planner_state(state)
    assert planner_state.best is not None and planner_state.best.candidate_id == "r0c1"
    assert orch._base(state, planner_state) == state.candidates["r0c3"]


def test_planner_feedback_includes_corner_dc_failure_without_replacing_ac_best(
    tmp_path: Path,
) -> None:
    orch = build(tmp_path, run_id="dc-feedback").orch
    state = orch._load_or_start()
    cid = "r0c1"
    state.results[f"{cid}|typical|op"] = fake_dc_result(orchestrator.TYPICAL)
    state.results[f"{cid}|typical"] = fake_result(orchestrator.TYPICAL, PASS)
    state.results[f"{cid}|hot_low_supply|op"] = fake_dc_result(
        orchestrator.CORNERS[1], failed_device="m5",
    )

    planner_state = orch._planner_state(state)

    assert planner_state.best is not None
    assert planner_state.best.candidate_id == cid
    assert planner_state.best.metrics == PASS
    assert [(row.condition, row.metrics is not None, row.passed) for row in planner_state.latest] == [
        ("hot_low_supply", False, False),
        ("typical", True, True),
    ]
    assert "m5: vds=0.1 V < vdsat=0.2 V" in planner_state.latest[0].issues[0]


def test_base_skips_dropped_candidates(tmp_path: Path) -> None:
    orch = build(tmp_path, run_id="fallback").orch
    state = orch._load_or_start()
    state.execution["decisions"] = {"r0c1": "drop"}
    assert orch._base(state, orch._planner_state(state)) == state.candidates["r0c2"]


def test_drop_is_excluded_from_winner(tmp_path: Path) -> None:
    orch = build(tmp_path, run_id="dropwin").orch
    state = orch._load_or_start()
    for cid in state.candidates:
        for condition in (orchestrator.TYPICAL, *orchestrator.CORNERS[1:]):
            state.results[f"{cid}|{condition.name}"] = fake_result(condition, PASS)
    assert orch._winner(state) == "r0c1"
    state.execution["decisions"] = {"r0c1": "drop"}
    assert orch._winner(state) == "r0c2"
    assert all(row.candidate_id != "r0c1" for row in orch._rows(state))


def test_bad_netlist_exits_2_before_storage(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "bad.cir"
    bad.write_text("* no sizing annotations\n", encoding="utf-8")
    assert main(["--task", "easy", "--netlist", str(bad)]) == 2
    assert "ORCHESTRATOR FAIL" in capsys.readouterr().err
