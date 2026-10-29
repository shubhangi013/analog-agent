from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

from pydantic import ValidationError
import pytest

from agent.sim import Condition
from agent.tracer import TraceEvent, Tracer


def test_simulation_event_has_required_fields(tmp_path: Path) -> None:
    tracer = Tracer("task", "run", tmp_path)
    tracer.emit(TraceEvent(
        type="simulation", duration_ms=20, inputs={"analysis": "ac"},
        outputs={"status": "ok"}, fingerprint="a" * 64, condition=Condition(),
        cache_hit=False, queue_wait_ms=2, run_ms=18, status="ok",
    ))
    event = json.loads(tracer.path.read_text())
    assert event.keys() == {
        "type", "timestamp", "duration_ms", "inputs", "outputs", "fingerprint",
        "condition", "cache_hit", "queue_wait_ms", "run_ms", "status",
    }
    assert event["timestamp"].endswith("Z")
    assert event["condition"]["temperature_c"] == 27
    assert event["cache_hit"] is False


@pytest.mark.parametrize("event_type", [
    "task_start", "model_call", "tool_call", "round_summary",
])
def test_common_events_and_newline_escaping(tmp_path: Path, event_type: str) -> None:
    tracer = Tracer("task", "run", tmp_path)
    tracer.emit(TraceEvent(type=event_type, inputs={"summary": "line 1\nline 2"}))
    lines = tracer.path.read_text().splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["type"] == event_type
    assert event["inputs"]["summary"] == "line 1\nline 2"
    assert set(event) == {"type", "timestamp", "duration_ms", "inputs", "outputs"}


def test_task_end_requires_accounting_and_engineer_verdict() -> None:
    with pytest.raises(ValidationError, match="Task-end"):
        TraceEvent(type="task_end")
    event = TraceEvent(type="task_end", outputs={
        "outcome": "cache_demo_pass", "simulations_used": 1, "wall_clock_s": 0.5,
        "cache_hits": 1, "engineer_verdict": "not_requested_cache_demo",
    })
    assert event.outputs["cache_hits"] == 1


def test_simulation_metadata_cannot_be_omitted() -> None:
    with pytest.raises(ValidationError, match="Simulation events"):
        TraceEvent(type="simulation")


@pytest.mark.parametrize("identifier", ["../outside", "..\\outside", "", "task/run", "task:run"])
def test_unsafe_filenames_rejected(tmp_path: Path, identifier: str) -> None:
    with pytest.raises(ValueError, match="safe filename"):
        Tracer(identifier, "run", tmp_path)


def test_trace_reopening_appends(tmp_path: Path) -> None:
    Tracer("task", "run", tmp_path).emit(TraceEvent(type="task_start"))
    reopened = Tracer("task", "run", tmp_path)
    reopened.emit(TraceEvent(type="round_summary"))
    assert len(reopened.path.read_text().splitlines()) == 2


def test_parallel_writes_produce_complete_json_lines(tmp_path: Path) -> None:
    tracer = Tracer("task", "run", tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda n: tracer.emit(TraceEvent(type="tool_call", inputs={"n": n})),
                      range(20)))
    events = [json.loads(line) for line in tracer.path.read_text().splitlines()]
    assert len(events) == 20
    assert sorted(event["inputs"]["n"] for event in events) == list(range(20))


def test_nonfinite_json_values_are_rejected(tmp_path: Path) -> None:
    tracer = Tracer("task", "run", tmp_path)
    with pytest.raises(ValueError):
        tracer.emit(TraceEvent(type="tool_call", outputs={"gain_db": float("nan")}))
    assert not tracer.path.exists()


def test_io_failures_are_not_silenced(tmp_path: Path) -> None:
    tracer = Tracer("task", "run", tmp_path)
    tracer.path.mkdir()
    with pytest.raises(OSError):
        tracer.emit(TraceEvent(type="task_start"))
