"""Typed, append-only JSONL events; never include raw simulator output."""

from datetime import datetime, timezone
from collections.abc import Callable
import json
import os
from pathlib import Path
import re
import time
from threading import Lock
from typing import Literal, ParamSpec, TypeVar

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from agent.sim import Condition
from agent.smoke import ROOT


EventType = Literal[
    "task_start", "model_call", "tool_call", "simulation", "round_summary", "export", "task_end"
]
P = ParamSpec("P")
T = TypeVar("T")


def json_text(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class TraceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    type: EventType
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    duration_ms: float = Field(default=0, ge=0)
    inputs: dict[str, JsonValue] = Field(default_factory=dict)
    outputs: dict[str, JsonValue] = Field(default_factory=dict)
    fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    condition: Condition | None = None
    cache_hit: bool | None = None
    queue_wait_ms: float | None = Field(default=None, ge=0)
    run_ms: float | None = Field(default=None, ge=0)
    status: Literal["ok", "error", "timeout"] | None = None

    @model_validator(mode="after")
    def require_simulation_fields(self) -> "TraceEvent":
        if self.type == "simulation" and any(
            value is None for value in (
                self.fingerprint, self.condition, self.cache_hit,
                self.queue_wait_ms, self.run_ms, self.status,
            )
        ):
            raise ValueError("Simulation events require fingerprint and timing/status fields.")
        if self.type == "task_end":
            required = {
                "outcome", "simulations_used", "wall_clock_s", "cache_hits", "engineer_verdict",
            }
            if not required <= self.outputs.keys():
                raise ValueError("Task-end events require outcome, usage and engineer verdict.")
        return self


class Tracer:
    def __init__(
        self, task_id: str, run_id: str, trace_dir: Path = ROOT / "traces"
    ) -> None:
        for value in (task_id, run_id):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,100}", value):
                raise ValueError("Trace task/run identifiers must be safe filename components.")
        trace_dir.mkdir(parents=True, exist_ok=True)
        self.path = trace_dir / f"{task_id}_{run_id}.jsonl"
        self._lock = Lock()

    def emit(self, event: TraceEvent) -> None:
        line = json_text(event.model_dump(mode="json", exclude_none=True)) + "\n"
        with self._lock, self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())

    def tool(self, name: str, function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
        started = time.perf_counter()
        try:
            value = function(*args, **kwargs)
        except Exception as error:
            self.emit(TraceEvent(
                type="tool_call", duration_ms=(time.perf_counter() - started) * 1000,
                inputs={"tool": name}, outputs={"status": "error", "error": str(error)[:500]},
            ))
            raise
        outputs: dict[str, JsonValue] = {"status": "ok"}
        if isinstance(value, (str, bool, int, float)) or value is None:
            outputs["value"] = value
        elif isinstance(value, BaseModel):
            outputs["value"] = value.model_dump(mode="json")
        elif isinstance(value, (list, tuple, dict)):
            outputs["items"] = len(value)
        inputs: dict[str, JsonValue] = {"tool": name}
        if name == "calc" and args and isinstance(args[0], str):
            inputs["expression"] = args[0]
        self.emit(TraceEvent(type="tool_call", duration_ms=(time.perf_counter() - started) * 1000,
                             inputs=inputs, outputs=outputs))
        return value
