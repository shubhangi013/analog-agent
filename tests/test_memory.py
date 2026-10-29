from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
from threading import Barrier, Lock
import time

from pydantic import ValidationError
import pytest

from agent import memory as storage
from agent.memory import CachedSimulator, Memory, WorkingState, fingerprint
from agent.sim import BiasPoint, Candidate, Condition, Metrics, SimulationResult
from agent.tracer import Tracer
from agent.verifier import Targets


@pytest.fixture
def candidate() -> Candidate:
    return Candidate(
        W_in=10, L_in=1, W_load=5, L_load=1, W_tail=5, L_tail=1, I_bias=25
    )


@pytest.fixture
def targets() -> Targets:
    return Targets(gain_db=30, ugf_hz=20e6, pm_deg=60, power_w=200e-6)


@pytest.fixture
def result(tmp_path: Path) -> SimulationResult:
    run_dir = tmp_path / "raw_run"
    run_dir.mkdir()
    (run_dir / "ngspice.log").write_text("RAW_LOG_MUST_STAY_ON_DISK", encoding="utf-8")
    return SimulationResult(
        status="ok", condition=Condition(), analysis="ac",
        metrics=Metrics(gain_db=36, ugf_hz=25e6, pm_deg=75, power_w=45e-6),
        bias={f"m{i}": BiasPoint(vds=0.5, vdsat=0.2, vgs=0.6, vth=0.4, id_a=12.5e-6)
              for i in range(1, 6)},
        run_dir=str(run_dir),
    )


@pytest.fixture
def memory(tmp_path: Path) -> Memory:
    return Memory(tmp_path / "memory.sqlite3")


@pytest.fixture
def engine(
    memory: Memory, result: SimulationResult, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[CachedSimulator, list[tuple[str, bytes]]]:
    monkeypatch.setattr(storage, "ROOT", tmp_path)
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "ptm180.lib").write_bytes(b"model version 1")
    monkeypatch.setattr(storage, "find_ngspice", lambda: "ngspice-test")
    monkeypatch.setattr(storage, "ngspice_version", lambda executable: "ngspice-test-1")
    calls: list[tuple[str, bytes]] = []
    lock = Lock()

    def fake_simulate(
        candidate: Candidate, condition: Condition, *, analysis: str, timeout_s: float,
        netlist: str, model_contents: bytes, executable: str,
    ) -> SimulationResult:
        with lock:
            calls.append((netlist, model_contents))
        time.sleep(0.03)
        answer = result.model_copy(deep=True)
        answer.condition = condition
        answer.analysis = analysis
        return answer

    monkeypatch.setattr(storage, "simulate", fake_simulate)
    tracer = Tracer("test", "run", tmp_path / "traces")
    return CachedSimulator(memory, tracer), calls


def test_identical_requests_use_one_simulation(
    engine: tuple[CachedSimulator, list[tuple[str, bytes]]], candidate: Candidate,
) -> None:
    simulator, calls = engine
    first = simulator.simulate(candidate)
    second = simulator.simulate(candidate, timeout_s=60)
    assert not first.cache_hit and second.cache_hit
    assert first.fingerprint == second.fingerprint
    assert first.result == second.result
    assert len(calls) == simulator.simulations_used == simulator.cache_hits == 1
    assert second.run_ms == 0
    with simulator.memory.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM results_store").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM simulation_claims").fetchone()[0] == 0


def test_cache_survives_reopening(
    engine: tuple[CachedSimulator, list[tuple[str, bytes]]],
    candidate: Candidate, tmp_path: Path,
) -> None:
    simulator, calls = engine
    first = simulator.simulate(candidate)
    reopened = CachedSimulator(
        Memory(simulator.memory.path), Tracer("test", "reopened", tmp_path / "traces")
    )
    second = reopened.simulate(candidate)
    assert second.cache_hit
    assert second.result == first.result
    assert reopened.simulations_used == 0
    assert len(calls) == 1


def test_cache_trace_records_both_requests(
    engine: tuple[CachedSimulator, list[tuple[str, bytes]]], candidate: Candidate,
) -> None:
    simulator, _ = engine
    first = simulator.simulate(candidate)
    simulator.simulate(candidate)
    events = [json.loads(line) for line in simulator.tracer.path.read_text().splitlines()]
    assert [event["type"] for event in events] == [
        "simulation", "tool_call", "simulation", "tool_call",
    ]
    simulations = [event for event in events if event["type"] == "simulation"]
    assert [event["cache_hit"] for event in simulations] == [False, True]
    assert all(event["fingerprint"] == first.fingerprint for event in simulations)
    assert all(event["queue_wait_ms"] >= 0 for event in simulations)
    assert simulations[0]["run_ms"] > 0
    assert simulations[1]["run_ms"] == 0
    assert simulations[0]["outputs"]["metrics"]["gain_db"] == 36


@pytest.mark.parametrize("change", ["candidate", "condition", "model", "version", "template", "analysis"])
def test_input_changes_invalidate_cache(
    engine: tuple[CachedSimulator, list[tuple[str, bytes]]], candidate: Candidate,
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    simulator, calls = engine
    first = simulator.simulate(candidate)
    condition = Condition()
    analysis = "ac"
    if change == "candidate":
        candidate = candidate.model_copy(update={"W_in": 11})
    elif change == "condition":
        condition = Condition(name="hot_low", temperature_c=125, supply_v=1.62)
    elif change == "model":
        (tmp_path / "models" / "ptm180.lib").write_bytes(b"model version 2")
    elif change == "version":
        monkeypatch.setattr(storage, "ngspice_version", lambda executable: "ngspice-test-2")
    elif change == "template":
        render = storage.render_netlist
        monkeypatch.setattr(
            storage, "render_netlist",
            lambda c, k, a, circuit=None: "* changed template\n" + render(c, k, a, circuit),
        )
    elif change == "analysis":
        analysis = "op"
    second = simulator.simulate(candidate, condition, analysis=analysis)
    assert not second.cache_hit
    assert second.fingerprint != first.fingerprint
    assert len(calls) == 2


def test_fingerprint_is_canonical_and_unambiguous() -> None:
    condition = Condition()
    expected = fingerprint("netlist", b"model", "version", condition)
    assert len(expected) == 64
    reordered = Condition.model_validate(dict(reversed(list(condition.model_dump().items()))))
    assert fingerprint("netlist", b"model", "version", reordered) == expected
    assert fingerprint(
        "netlist", b"model", "version", Condition(temperature_c=27.0)
    ) == expected
    assert fingerprint("ab", b"c", "version", condition) != fingerprint(
        "a", b"bc", "version", condition
    )


def test_simulator_receives_exact_fingerprinted_snapshots(
    engine: tuple[CachedSimulator, list[tuple[str, bytes]]], candidate: Candidate,
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    simulator, calls = engine
    fake = storage.simulate

    def changing_model(*args: object, **kwargs: object) -> SimulationResult:
        (tmp_path / "models" / "ptm180.lib").write_bytes(b"changed during execution")
        return fake(*args, **kwargs)

    monkeypatch.setattr(storage, "simulate", changing_model)
    answer = simulator.simulate(candidate)
    netlist, model = calls[0]
    assert model == b"model version 1"
    assert answer.fingerprint == fingerprint(netlist, model, "ngspice-test-1", Condition())


def test_concurrent_identical_requests_share_a_claim(
    engine: tuple[CachedSimulator, list[tuple[str, bytes]]],
    candidate: Candidate, tmp_path: Path,
) -> None:
    first, calls = engine
    second = CachedSimulator(
        Memory(first.memory.path), Tracer("test", "second", tmp_path / "traces")
    )
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit((first if i % 2 else second).simulate, candidate)
                   for i in range(4)]
        answers = [future.result(timeout=10) for future in futures]
    assert len(calls) == 1
    assert sum(answer.cache_hit for answer in answers) == 3
    assert first.simulations_used + second.simulations_used == 1


def test_distinct_candidates_can_run_in_parallel(
    engine: tuple[CachedSimulator, list[tuple[str, bytes]]], candidate: Candidate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    simulator, calls = engine
    barrier = Barrier(2)
    fake = storage.simulate

    def parallel_simulation(*args: object, **kwargs: object) -> SimulationResult:
        barrier.wait(timeout=5)
        return fake(*args, **kwargs)

    monkeypatch.setattr(storage, "simulate", parallel_simulation)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(simulator.simulate, candidate),
            pool.submit(simulator.simulate, candidate.model_copy(update={"W_in": 11})),
        ]
        assert all(future.result(timeout=10).result.status == "ok" for future in futures)
    assert len(calls) == 2


@pytest.mark.parametrize("status", ["error", "timeout"])
def test_failed_attempt_is_stored_but_retried(
    engine: tuple[CachedSimulator, list[tuple[str, bytes]]], candidate: Candidate,
    result: SimulationResult, status: str,
) -> None:
    simulator, calls = engine
    result.status = status
    result.error = "transient simulator failure"
    first = simulator.simulate(candidate)
    result.status = "ok"
    result.error = None
    second = simulator.simulate(candidate)
    third = simulator.simulate(candidate)
    assert first.result.status == status
    assert not first.cache_hit and not second.cache_hit and third.cache_hit
    assert len(calls) == 2
    with simulator.memory.connect() as connection:
        rows = connection.execute("SELECT status FROM results_store ORDER BY id").fetchall()
    assert [row["status"] for row in rows] == [status, "ok"]


def test_expired_claim_can_be_recovered(
    memory: Memory, candidate: Candidate, result: SimulationResult,
) -> None:
    assert memory.claim("key", "old", 30) == (None, True)
    assert memory.claim("key", "new", 30) == (None, False)
    with memory.connect() as connection:
        connection.execute("UPDATE simulation_claims SET lease_until = 0")
    assert memory.claim("key", "new", 30) == (None, True)
    memory.release("key", "old")
    with pytest.raises(RuntimeError, match="claim was lost"):
        memory.finish("key", "old", candidate, "version", result)
    memory.finish("key", "new", candidate, "version", result)
    assert memory.cached_result("key") == result


def test_unexpected_execution_exception_releases_claim(
    engine: tuple[CachedSimulator, list[tuple[str, bytes]]], candidate: Candidate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    simulator, _ = engine

    def fail(*args: object, **kwargs: object) -> SimulationResult:
        raise OSError("disk full")

    monkeypatch.setattr(storage, "simulate", fail)
    with pytest.raises(OSError, match="disk full"):
        simulator.simulate(candidate)
    with simulator.memory.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM simulation_claims").fetchone()[0] == 0
    events = [json.loads(line) for line in simulator.tracer.path.read_text().splitlines()]
    assert events[-1]["type"] == "tool_call"
    assert events[-1]["outputs"]["status"] == "error"


def test_raw_log_not_stored_in_database_or_trace(
    engine: tuple[CachedSimulator, list[tuple[str, bytes]]], candidate: Candidate,
) -> None:
    simulator, _ = engine
    answer = simulator.simulate(candidate)
    with simulator.memory.connect() as connection:
        row = connection.execute("SELECT result_json FROM results_store").fetchone()
    assert "RAW_LOG_MUST_STAY_ON_DISK" not in row[0]
    assert "RAW_LOG_MUST_STAY_ON_DISK" not in simulator.tracer.path.read_text()
    assert answer.result.run_dir in json.loads(row[0])["run_dir"]


def test_corrupt_cache_is_an_explicit_error(
    engine: tuple[CachedSimulator, list[tuple[str, bytes]]], candidate: Candidate,
) -> None:
    simulator, calls = engine
    simulator.simulate(candidate)
    with simulator.memory.connect() as connection:
        connection.execute("UPDATE results_store SET result_json = '{invalid'")
    with pytest.raises(ValueError):
        simulator.simulate(candidate)
    assert len(calls) == 1


def test_invalid_candidate_is_logged_without_invalid_json(
    engine: tuple[CachedSimulator, list[tuple[str, bytes]]], candidate: Candidate,
) -> None:
    simulator, calls = engine
    invalid = candidate.model_copy(update={"W_in": float("nan")})
    with pytest.raises(ValidationError, match="W_in"):
        simulator.simulate(invalid)
    assert calls == []
    event = json.loads(simulator.tracer.path.read_text())
    assert event["outputs"]["status"] == "error"
    assert "W_in" in event["outputs"]["error"]


def test_checkpoint_versions_are_durable_snapshots(
    memory: Memory, candidate: Candidate, targets: Targets, result: SimulationResult,
) -> None:
    state = WorkingState(step="read_request", targets=targets, candidates={"c1": candidate})
    assert memory.load_checkpoint("task", "run") is None
    assert memory.save_checkpoint("task", "run", state) == 1
    state.step = "simulate"
    state.results["c1_typical"] = result
    state.simulations_used = 1
    state.assumptions.append("Test assumption")
    assert memory.save_checkpoint("task", "run", state) == 2
    latest = Memory(memory.path).load_checkpoint("task", "run")
    assert latest is not None and latest.version == 2 and latest.state == state
    assert memory.load_checkpoint("task", "other_run") is None
    with memory.connect() as connection:
        first = connection.execute(
            "SELECT state_json FROM working_memory WHERE version = 1"
        ).fetchone()
    assert json.loads(first[0])["results"] == {}
    assert json.loads(first[0])["step"] == "read_request"


def test_concurrent_checkpoint_versions_do_not_collide(memory: Memory, targets: Targets) -> None:
    state = WorkingState(step="test", targets=targets)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(memory.save_checkpoint, "task", "run", state) for _ in range(8)]
        versions = [future.result(timeout=10) for future in futures]
    assert sorted(versions) == list(range(1, 9))


def all_conditions(result: SimulationResult) -> list[SimulationResult]:
    return [
        result.model_copy(update={"condition": Condition(
            name=name, temperature_c=temperature, supply_v=supply,
        )}, deep=True)
        for name, temperature, supply in (
            ("typical", 27, 1.8), ("hot_low", 125, 1.62), ("cold_low", -40, 1.62),
            ("hot_high", 125, 1.98), ("cold_high", -40, 1.98),
        )
    ]


def test_only_approved_all_condition_passing_designs_are_stored(
    memory: Memory, candidate: Candidate, targets: Targets, result: SimulationResult,
) -> None:
    conditions = all_conditions(result)
    with pytest.raises(ValueError, match="engineer-approved"):
        memory.store_design(candidate, targets, conditions, approved=False)
    with pytest.raises(ValueError, match="all five"):
        memory.store_design(candidate, targets, [result], approved=True)
    assert memory.designs() == []
    design_id = memory.store_design(candidate, targets, conditions, approved=True)
    stored = Memory(memory.path).designs()
    assert len(stored) == 1 and stored[0].id == design_id
    assert stored[0].candidate == candidate and stored[0].results == conditions


def test_failing_or_duplicate_conditions_cannot_enter_library(
    memory: Memory, candidate: Candidate, targets: Targets, result: SimulationResult,
) -> None:
    conditions = all_conditions(result)
    conditions[-1].bias["m5"].vds = 0.01
    with pytest.raises(ValueError, match="pass verification"):
        memory.store_design(candidate, targets, conditions, approved=True)
    with pytest.raises(ValueError, match="all five"):
        memory.store_design(candidate, targets, [result] * 5, approved=True)
    assert memory.designs() == []


def test_lessons_are_unvalidated_by_default(memory: Memory) -> None:
    memory.store_lesson("task", "Proposed lesson")
    with pytest.raises(ValueError, match="held-out"):
        memory.store_lesson("task", "Cannot self-approve a lesson", validated=True)
    assert memory.lessons() == []
    assert memory.lessons(validated_only=False) == ["Proposed lesson"]
    with pytest.raises(ValueError):
        memory.store_lesson("task", " ")


def test_repeated_cache_hits_for_one_resumed_job_are_not_lost(
    memory: Memory, result: SimulationResult,
) -> None:
    memory.save_job("task", "run", "export-typical", result, cache_hit=True, done=True)
    memory.save_job("task", "run", "export-typical", result, cache_hit=True, done=True)
    assert memory.task_counts("task", "run") == (0, 2)


def test_negative_checkpoint_counters_rejected(targets: Targets) -> None:
    with pytest.raises(ValidationError):
        WorkingState(step="test", targets=targets, simulations_used=-1)


def test_old_design_library_gains_circuit_sha_and_keeps_rows(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE design_library (id INTEGER PRIMARY KEY, candidate_json TEXT NOT NULL,"
        " targets_json TEXT NOT NULL, results_json TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO design_library (candidate_json, targets_json, results_json, created_at)"
        " VALUES ('{}', '{}', '[]', '2024-01-01T00:00:00+00:00')"
    )
    connection.commit()
    connection.close()

    memory = Memory(path)
    with memory.connect() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(design_library)")}
        sha = connection.execute("SELECT circuit_sha FROM design_library").fetchone()[0]
    assert "circuit_sha" in columns
    assert {"circuit_body", "provenance_json"} <= columns
    assert sha == ""


def test_design_stores_circuit_body_and_provenance(
    memory: Memory, candidate: Candidate, targets: Targets, result: SimulationResult,
) -> None:
    design_id = memory.store_design(
        candidate, targets, all_conditions(result), approved=True,
        circuit_sha="a" * 64, circuit_body="* body\n", provenance={"task_id": "easy"},
    )
    design = memory.design(design_id)
    assert design.circuit_body == "* body\n"
    assert design.circuit_sha == "a" * 64
    assert design.provenance == {"task_id": "easy"}


def test_missing_design_id_raises_key_error(memory: Memory) -> None:
    with pytest.raises(KeyError):
        memory.design(999)
