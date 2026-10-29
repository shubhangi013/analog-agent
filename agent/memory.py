"""Durable checkpoints and simulation results in one SQLite database."""

from contextlib import contextmanager
from collections.abc import Callable, Generator
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import subprocess
from threading import Lock
import time
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from agent.sim import (
    Analysis, Candidate, Condition, SimulationResult, ngspice_version, render_netlist, simulate,
)
from agent.smoke import ROOT, find_ngspice
from agent.tracer import TraceEvent, Tracer, json_text
from agent.verifier import Targets, verify


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fingerprint(
    netlist: str, model_contents: bytes, version: str, condition: Condition
) -> str:
    """Length-prefix each component so boundaries cannot produce hash collisions."""
    condition = Condition.model_validate(condition.model_dump())
    digest = hashlib.sha256()
    for part in (
        b"mini-agent-cache-v4-held-bias-validation", netlist.encode("utf-8"), model_contents,
        version.encode("utf-8"), json_text(condition.model_dump()).encode("utf-8"),
    ):
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()


class WorkingState(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    step: str
    targets: Targets
    circuit_sha: str = ""
    assumptions: list[str] = Field(default_factory=list)
    candidates: dict[str, Candidate] = Field(default_factory=dict)
    results: dict[str, SimulationResult] = Field(default_factory=dict)
    best_candidate_id: str | None = None
    decisions: list[str] = Field(default_factory=list)
    round_summaries: list[str] = Field(default_factory=list)
    simulations_used: int = Field(default=0, ge=0)
    cache_hits: int = Field(default=0, ge=0)
    execution: dict[str, JsonValue] = Field(default_factory=dict)


class BudgetExhausted(RuntimeError):
    pass


class RetryExhausted(RuntimeError):
    pass


class Checkpoint(BaseModel):
    task_id: str
    run_id: str
    version: int
    state: WorkingState


class StoredDesign(BaseModel):
    id: int
    candidate: Candidate
    targets: Targets
    results: list[SimulationResult]
    circuit_sha: str = ""
    circuit_body: str = ""
    provenance: dict[str, JsonValue] = Field(default_factory=dict)


def _design_from_row(row: sqlite3.Row) -> StoredDesign:
    return StoredDesign(
        id=row["id"], candidate=Candidate.model_validate_json(row["candidate_json"]),
        targets=Targets.model_validate_json(row["targets_json"]),
        results=[SimulationResult.model_validate(item)
                 for item in json.loads(row["results_json"])],
        circuit_sha=row["circuit_sha"], circuit_body=row["circuit_body"],
        provenance=json.loads(row["provenance_json"]),
    )


class Memory:
    def __init__(self, path: Path = ROOT / "runs" / "memory.sqlite3") -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS working_memory (
                    task_id TEXT NOT NULL, run_id TEXT NOT NULL,
                    version INTEGER NOT NULL, state_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (task_id, run_id, version)
                );
                CREATE TABLE IF NOT EXISTS results_store (
                    id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL,
                    candidate_json TEXT NOT NULL, condition_json TEXT NOT NULL,
                    analysis TEXT NOT NULL, ngspice_version TEXT NOT NULL,
                    status TEXT NOT NULL, result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS results_by_fingerprint
                    ON results_store (fingerprint, status, id);
                CREATE TABLE IF NOT EXISTS simulation_claims (
                    fingerprint TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    lease_until REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS design_library (
                    id INTEGER PRIMARY KEY, candidate_json TEXT NOT NULL,
                    targets_json TEXT NOT NULL, results_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lessons (
                    id INTEGER PRIMARY KEY, task_id TEXT NOT NULL,
                    text TEXT NOT NULL, validated INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_attempts (
                    id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, run_id TEXT NOT NULL,
                    job_id TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS attempts_by_run
                    ON task_attempts(task_id, run_id, job_id);
                CREATE TABLE IF NOT EXISTS task_jobs (
                    task_id TEXT NOT NULL, run_id TEXT NOT NULL, job_id TEXT NOT NULL,
                    result_json TEXT NOT NULL, cache_hit INTEGER NOT NULL,
                    done INTEGER NOT NULL,
                    PRIMARY KEY(task_id, run_id, job_id)
                );
            """)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(design_library)")}
            if "circuit_sha" not in columns:
                connection.execute(
                    "ALTER TABLE design_library ADD COLUMN circuit_sha TEXT NOT NULL DEFAULT ''"
                )
            if "circuit_body" not in columns:
                connection.execute(
                    "ALTER TABLE design_library ADD COLUMN circuit_body TEXT NOT NULL DEFAULT ''"
                )
            if "provenance_json" not in columns:
                connection.execute(
                    "ALTER TABLE design_library ADD COLUMN provenance_json TEXT NOT NULL DEFAULT '{}'"
                )
            lesson_columns = {row["name"] for row in connection.execute("PRAGMA table_info(lessons)")}
            for name, declaration in (
                ("candidate_json", "TEXT"), ("circuit_sha", "TEXT NOT NULL DEFAULT ''"),
                ("provenance", "TEXT NOT NULL DEFAULT ''"),
                ("validation_json", "TEXT NOT NULL DEFAULT '{}'"),
            ):
                if name not in lesson_columns:
                    connection.execute(f"ALTER TABLE lessons ADD COLUMN {name} {declaration}")
            job_columns = {row["name"] for row in connection.execute("PRAGMA table_info(task_jobs)")}
            if "cache_hits" not in job_columns:
                connection.execute("ALTER TABLE task_jobs ADD COLUMN cache_hits INTEGER NOT NULL DEFAULT 0")
                connection.execute("UPDATE task_jobs SET cache_hits=cache_hit WHERE done=1")

    def reserve_simulation(
        self, task_id: str, run_id: str, job_id: str, budget: int, max_attempts: int,
    ) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            used = connection.execute(
                "SELECT COUNT(*) FROM task_attempts WHERE task_id=? AND run_id=?",
                (task_id, run_id),
            ).fetchone()[0]
            attempts = connection.execute(
                "SELECT COUNT(*) FROM task_attempts WHERE task_id=? AND run_id=? AND job_id=?",
                (task_id, run_id, job_id),
            ).fetchone()[0]
            if used >= budget:
                raise BudgetExhausted(f"Simulation budget {budget} exhausted.")
            if attempts >= max_attempts:
                raise RetryExhausted(f"Job {job_id} exhausted {max_attempts} attempts.")
            connection.execute(
                "INSERT INTO task_attempts(task_id,run_id,job_id,created_at) VALUES(?,?,?,?)",
                (task_id, run_id, job_id, utc_now()),
            )

    def save_job(
        self, task_id: str, run_id: str, job_id: str, result: SimulationResult,
        *, cache_hit: bool, done: bool,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO task_jobs(task_id,run_id,job_id,result_json,cache_hit,done,cache_hits) "
                "VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(task_id,run_id,job_id) DO UPDATE SET "
                "result_json=excluded.result_json, cache_hit=excluded.cache_hit, done=excluded.done, "
                "cache_hits=task_jobs.cache_hits+excluded.cache_hits",
                (task_id, run_id, job_id, json_text(result.model_dump(mode="json")),
                 int(cache_hit), int(done), int(cache_hit)),
            )

    def load_job(
        self, task_id: str, run_id: str, job_id: str,
    ) -> tuple[SimulationResult, bool, bool] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT result_json,cache_hit,done FROM task_jobs "
                "WHERE task_id=? AND run_id=? AND job_id=?", (task_id, run_id, job_id),
            ).fetchone()
        return None if row is None else (
            SimulationResult.model_validate_json(row["result_json"]),
            bool(row["cache_hit"]), bool(row["done"]),
        )

    def task_counts(self, task_id: str, run_id: str) -> tuple[int, int]:
        with self.connect() as connection:
            used = connection.execute(
                "SELECT COUNT(*) FROM task_attempts WHERE task_id=? AND run_id=?",
                (task_id, run_id),
            ).fetchone()[0]
            hits = connection.execute(
                "SELECT COALESCE(SUM(cache_hits),0) FROM task_jobs "
                "WHERE task_id=? AND run_id=?",
                (task_id, run_id),
            ).fetchone()[0]
        return used, hits

    def job_attempts(self, task_id: str, run_id: str, job_id: str) -> int:
        with self.connect() as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM task_attempts WHERE task_id=? AND run_id=? AND job_id=?",
                (task_id, run_id, job_id),
            ).fetchone()[0]

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection, None, None]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def save_checkpoint(self, task_id: str, run_id: str, state: WorkingState) -> int:
        state = WorkingState.model_validate(state.model_dump())
        payload = json_text(state.model_dump(mode="json"))
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM working_memory "
                "WHERE task_id = ? AND run_id = ?", (task_id, run_id),
            ).fetchone()
            version = int(row[0])
            connection.execute(
                "INSERT INTO working_memory VALUES (?, ?, ?, ?, ?)",
                (task_id, run_id, version, payload, utc_now()),
            )
        return version

    def load_checkpoint(self, task_id: str, run_id: str) -> Checkpoint | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT version, state_json FROM working_memory "
                "WHERE task_id = ? AND run_id = ? ORDER BY version DESC LIMIT 1",
                (task_id, run_id),
            ).fetchone()
        if row is None:
            return None
        return Checkpoint(
            task_id=task_id, run_id=run_id, version=row["version"],
            state=WorkingState.model_validate_json(row["state_json"]),
        )

    @staticmethod
    def _cached(
        connection: sqlite3.Connection, key: str
    ) -> SimulationResult | None:
        row = connection.execute(
            "SELECT result_json FROM results_store "
            "WHERE fingerprint = ? AND status = 'ok' ORDER BY id DESC LIMIT 1", (key,),
        ).fetchone()
        if row is None:
            return None
        result = SimulationResult.model_validate_json(row["result_json"])
        if result.status != "ok" or result.error is not None:
            raise ValueError(f"Corrupt cached result for fingerprint {key}")
        return result

    def cached_result(self, key: str) -> SimulationResult | None:
        with self.connect() as connection:
            return self._cached(connection, key)

    def claim(
        self, key: str, owner: str, lease_s: float
    ) -> tuple[SimulationResult | None, bool]:
        if not math.isfinite(lease_s) or lease_s <= 0:
            raise ValueError("Cache claim lease must be positive and finite.")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cached = self._cached(connection, key)
            if cached is not None:
                return cached, False
            now = time.time()
            row = connection.execute(
                "SELECT lease_until FROM simulation_claims WHERE fingerprint = ?", (key,)
            ).fetchone()
            if row is not None and row["lease_until"] > now:
                return None, False
            connection.execute(
                "INSERT INTO simulation_claims VALUES (?, ?, ?) "
                "ON CONFLICT(fingerprint) DO UPDATE SET "
                "owner = excluded.owner, lease_until = excluded.lease_until",
                (key, owner, now + lease_s),
            )
        return None, True

    def finish(
        self, key: str, owner: str, candidate: Candidate, version: str,
        result: SimulationResult,
    ) -> None:
        payload = json_text(result.model_dump(mode="json"))
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT owner FROM simulation_claims WHERE fingerprint = ?", (key,)
            ).fetchone()
            if row is None or row["owner"] != owner:
                raise RuntimeError("Simulation cache claim was lost; result was not cached.")
            connection.execute(
                "INSERT INTO results_store (fingerprint, candidate_json, condition_json, "
                "analysis, ngspice_version, status, result_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (key, json_text(candidate.model_dump()), json_text(result.condition.model_dump()),
                 result.analysis, version, result.status, payload, utc_now()),
            )
            connection.execute(
                "DELETE FROM simulation_claims WHERE fingerprint = ? AND owner = ?",
                (key, owner),
            )

    def release(self, key: str, owner: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM simulation_claims WHERE fingerprint = ? AND owner = ?",
                (key, owner),
            )

    def store_design(
        self, candidate: Candidate, targets: Targets,
        results: list[SimulationResult], *, approved: bool, circuit_sha: str = "",
        circuit_body: str = "", provenance: dict[str, JsonValue] | None = None,
    ) -> int:
        required = {(27, 1.8), (125, 1.62), (-40, 1.62), (125, 1.98), (-40, 1.98)}
        actual = {(r.condition.temperature_c, r.condition.supply_v) for r in results}
        if not approved:
            raise ValueError("Only engineer-approved designs can enter the design library.")
        if len(results) != 5 or actual != required:
            raise ValueError("A design must have results at all five required conditions.")
        if not all(verify(candidate, result, targets).passed for result in results):
            raise ValueError("A design must pass verification at every required condition.")
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO design_library (candidate_json, targets_json, results_json, "
                "created_at, circuit_sha, circuit_body, provenance_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (json_text(candidate.model_dump()), json_text(targets.model_dump()),
                 json_text([result.model_dump(mode="json") for result in results]), utc_now(),
                 circuit_sha, circuit_body, json_text(provenance or {})),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite did not return the inserted design id.")
            return cursor.lastrowid

    def designs(self) -> list[StoredDesign]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM design_library ORDER BY id").fetchall()
        return [_design_from_row(row) for row in rows]

    def design(self, design_id: int) -> StoredDesign:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM design_library WHERE id = ?", (design_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"No design with id {design_id}.")
        return _design_from_row(row)

    def store_lesson(
        self, task_id: str, text: str, *, validated: bool = False,
        candidate: Candidate | None = None, circuit_sha: str = "", provenance: str = "",
    ) -> int:
        if not text.strip() or len(text) > 1000:
            raise ValueError("A lesson must contain 1-1000 characters of short text.")
        if validated:
            raise ValueError("Validate lessons on the fixed held-out suite before promotion.")
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO lessons (task_id,text,validated,created_at,candidate_json,circuit_sha,"
                "provenance) VALUES (?,?,?,?,?,?,?)",
                (task_id, text, 0, utc_now(), candidate.model_dump_json() if candidate else None,
                 circuit_sha, provenance),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite did not return the inserted lesson id.")
            return cursor.lastrowid

    def lessons(self, *, validated_only: bool = True) -> list[str]:
        return [record["text"] for record in self.lesson_records(validated_only=validated_only)]

    def lesson_records(self, *, validated_only: bool = True) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM lessons WHERE validated >= ? ORDER BY id",
                (int(validated_only),),
            ).fetchall()
        records = [dict(row) for row in rows]
        if validated_only:
            from agent.lessons import accepts_validation
            records = [record for record in records
                       if record["candidate_json"] is not None
                       and accepts_validation(evidence := json.loads(record["validation_json"]))
                       and evidence["provenance"] == record["provenance"]
                       and evidence["candidate_sha"] == hashlib.sha256(
                           record["candidate_json"].encode()).hexdigest()]
        return records

    def promote_lesson(self, lesson_id: int, evidence: dict) -> bool:
        from agent.lessons import accepts_validation
        accepted = accepts_validation(evidence)
        with self.connect() as connection:
            row = connection.execute("SELECT candidate_json,provenance FROM lessons WHERE id=?",
                                     (lesson_id,)).fetchone()
            if row is None:
                raise KeyError(f"No lesson {lesson_id}.")
            accepted = accepted and row["candidate_json"] is not None and (
                row["provenance"] == evidence.get("provenance")) and (
                hashlib.sha256(row["candidate_json"].encode()).hexdigest()
                == evidence.get("candidate_sha"))
            connection.execute("UPDATE lessons SET validated=?,validation_json=? WHERE id=?",
                               (int(accepted), json_text(evidence), lesson_id))
        return accepted


@dataclass(frozen=True)
class CachedSimulation:
    result: SimulationResult
    fingerprint: str
    cache_hit: bool
    queue_wait_ms: float
    run_ms: float


class CachedSimulator:
    def __init__(self, memory: Memory, tracer: Tracer) -> None:
        self.memory = memory
        self.tracer = tracer
        self.simulations_used = 0
        self.cache_hits = 0
        self._counts_lock = Lock()

    def provenance(self) -> str:
        digest = hashlib.sha256()
        for path in ("models\\ptm180.lib", "circuits\\ota5t.cir.j2",
                     "agent\\sim.py", "agent\\verifier.py"):
            digest.update((ROOT / path).read_bytes())
        digest.update(ngspice_version(find_ngspice()).encode())
        return digest.hexdigest()

    def simulate(
        self, candidate: Candidate, condition: Condition | None = None, *,
        analysis: Analysis = "ac", timeout_s: float = 30,
        before_run: Callable[[], None] | None = None,
        queue_wait_ms: float = 0, circuit: str | None = None,
        netlist: str | None = None, model_contents: bytes | None = None,
    ) -> CachedSimulation:
        start = time.perf_counter()
        condition = condition if condition is not None else Condition()
        inputs: dict[str, JsonValue] = {"tool": "simulate", "analysis": analysis}
        if circuit is not None:
            inputs["circuit_sha"] = hashlib.sha256(circuit.encode()).hexdigest()
        if netlist is not None:
            inputs["netlist_sha"] = hashlib.sha256(netlist.encode()).hexdigest()
        try:
            candidate = Candidate.model_validate(candidate.model_dump())
            condition = Condition.model_validate(condition.model_dump())
            inputs["candidate"] = candidate.model_dump(mode="json")
            inputs["condition"] = condition.model_dump(mode="json")
            if netlist is None and model_contents is None:
                answer = self._simulate(candidate, condition, analysis, timeout_s, before_run, circuit)
            else:
                answer = self._simulate(
                    candidate, condition, analysis, timeout_s, before_run, circuit,
                    netlist=netlist, model_contents=model_contents,
                )
        except (OSError, sqlite3.Error, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            self.tracer.emit(TraceEvent(
                type="tool_call", duration_ms=(time.perf_counter() - start) * 1000,
                inputs=inputs, outputs={"status": "error", "error": str(error)[:500]},
            ))
            raise
        result = answer.result
        outputs: dict[str, JsonValue] = {
            "status": result.status,
            "metrics": result.metrics.model_dump(mode="json") if result.metrics else None,
            "error": result.error[:500] if result.error else None,
            "warning_count": len(result.warnings), "run_dir": result.run_dir,
            "bias": {name: point.model_dump() for name, point in result.bias.items()},
            "nodes": result.nodes, "dc_power_w": result.dc_power_w,
            "transient": result.transient.model_dump(mode="json") if result.transient else None,
            "output_v": result.output_v, "bias_voltage_v": result.bias_voltage_v,
            "tail_current_a": result.tail_current_a,
        }
        duration_ms = (time.perf_counter() - start) * 1000
        self.tracer.emit(TraceEvent(
            type="simulation", duration_ms=duration_ms, inputs=inputs, outputs=outputs,
            fingerprint=answer.fingerprint, condition=condition, cache_hit=answer.cache_hit,
            queue_wait_ms=answer.queue_wait_ms + queue_wait_ms,
            run_ms=answer.run_ms, status=result.status,
        ))
        self.tracer.emit(TraceEvent(
            type="tool_call", duration_ms=duration_ms, inputs=inputs,
            outputs={"status": result.status, "fingerprint": answer.fingerprint,
                     "cache_hit": answer.cache_hit},
        ))
        return answer

    def _simulate(
        self, candidate: Candidate, condition: Condition,
        analysis: Analysis, timeout_s: float,
        before_run: Callable[[], None] | None = None, circuit: str | None = None,
        *, netlist: str | None = None, model_contents: bytes | None = None,
    ) -> CachedSimulation:
        if analysis not in ("op", "ac", "tran"):
            raise ValueError("Analysis must be 'op', 'ac' or 'tran'.")
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("Simulation timeout must be positive and finite.")
        netlist = netlist if netlist is not None else render_netlist(candidate, condition, analysis, circuit)
        model_contents = model_contents if model_contents is not None else (
            ROOT / "models" / "ptm180.lib"
        ).read_bytes()
        executable = find_ngspice()
        version = ngspice_version(executable)
        key = fingerprint(netlist, model_contents, version, condition)
        owner = uuid4().hex
        waiting = time.perf_counter()
        while True:
            cached, claimed = self.memory.claim(key, owner, timeout_s + 60)
            if cached is not None or claimed:
                break
            if time.perf_counter() - waiting > timeout_s + 65:
                raise RuntimeError("Timed out waiting for an in-flight simulation cache claim.")
            time.sleep(0.02)
        queue_ms = (time.perf_counter() - waiting) * 1000
        if cached is not None:
            with self._counts_lock:
                self.cache_hits += 1
            return CachedSimulation(cached, key, True, queue_ms, 0)
        try:
            if before_run is not None:
                before_run()
            with self._counts_lock:
                self.simulations_used += 1
            started = time.perf_counter()
            result = simulate(
                candidate, condition, analysis=analysis, timeout_s=timeout_s,
                netlist=netlist, model_contents=model_contents, executable=executable,
            )
            run_ms = (time.perf_counter() - started) * 1000
            self.memory.finish(key, owner, candidate, version, result)
        finally:
            self.memory.release(key, owner)
        return CachedSimulation(result, key, False, queue_ms, run_ms)


def main() -> int:
    task_id, run_id = "cache_demo", uuid4().hex
    memory = Memory()
    tracer = Tracer(task_id, run_id)
    simulator = CachedSimulator(memory, tracer)
    targets = Targets(gain_db=30, ugf_hz=20e6, pm_deg=60, power_w=200e-6)
    candidate = Candidate(
        W_in=10, L_in=1, W_load=5, L_load=1, W_tail=5, L_tail=1, I_bias=25
    )
    state = WorkingState(step="read_request", targets=targets, candidates={"c1": candidate})
    start = time.perf_counter()
    tracer.emit(TraceEvent(type="task_start", inputs={"task_id": task_id, "run_id": run_id}))
    version1 = memory.save_checkpoint(task_id, run_id, state)
    try:
        for request in (1, 2):
            answer = simulator.simulate(candidate)
            if answer.result.status != "ok":
                raise RuntimeError(answer.result.error)
            state.results["c1_typical"] = answer.result
            print(f"Request {request}: cache_hit={answer.cache_hit}, status=ok")
        state.step = "round_summary"
        state.simulations_used = simulator.simulations_used
        state.cache_hits = simulator.cache_hits
        verdict = verify(candidate, answer.result, targets)
        state.best_candidate_id = "c1" if verdict.passed else None
        state.decisions.append(
            f"Typical passed={verdict.passed}; hard conditions not yet evaluated."
        )
        state.round_summaries.append("One candidate requested twice; reused identical result.")
        version2 = memory.save_checkpoint(task_id, run_id, state)
        tracer.emit(TraceEvent(
            type="round_summary",
            outputs={"passed_typical": verdict.passed,
                     "simulations_used": simulator.simulations_used,
                     "cache_hits": simulator.cache_hits},
        ))
        restored = Memory(memory.path).load_checkpoint(task_id, run_id)
        if (
            restored is None or restored.state != state or version1 != 1 or version2 != 2
            or not verdict.passed or simulator.simulations_used > 1 or simulator.cache_hits < 1
        ):
            raise RuntimeError("Cache or checkpoint acceptance check failed.")
        print(f"Simulations used: {simulator.simulations_used}")
        print(f"Cache hits: {simulator.cache_hits}")
        print(f"Checkpoint versions: {version1}, {version2}; reopened latest: {restored.version}")
        outcome = "cache_demo_pass"
    except (OSError, sqlite3.Error, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"CACHE FAIL: {error}")
        outcome = "cache_demo_fail"
    elapsed_ms = (time.perf_counter() - start) * 1000
    tracer.emit(TraceEvent(
        type="task_end", duration_ms=elapsed_ms,
        outputs={"outcome": outcome, "simulations_used": simulator.simulations_used,
                 "wall_clock_s": elapsed_ms / 1000, "cache_hits": simulator.cache_hits,
                 "engineer_verdict": "not_requested_cache_demo"},
    ))
    print(f"Database: {memory.path}")
    print(f"Trace: {tracer.path}")
    if outcome == "cache_demo_fail":
        return 1
    print("CACHE PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
