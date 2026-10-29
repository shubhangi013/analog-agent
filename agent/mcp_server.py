"""MCP adapter for the simulator-grounded circuit engineer."""

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from threading import Lock, Thread
from typing import Annotated, Any, Iterator, Literal
from uuid import uuid4

from mcp.server import MCPServer
from pydantic import Field

from agent.diagnostics import DEFAULT_CANDIDATE, run_diagnostic
from agent.intake import route_request
from agent.memory import CachedSimulator, Memory
from agent.netlist import load_circuit
from agent.orchestrator import BUDGET, Orchestrator, load_targets
from agent.planner import LLMConfig, Planner
from agent.sim import Candidate
from agent.smoke import ROOT
from agent.spec import Objective
from agent.tracer import Tracer, json_text


MCP_ROOT = ROOT / "runs" / "mcp"
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,100}")
IntakeMode = Literal["auto", "deterministic", "model"]
_active: set[str] = set()
_active_lock = Lock()
_jobs: dict[tuple[str, str], tuple[Thread, dict[str, Any]]] = {}
_jobs_lock = Lock()


def _run_id(value: str | None) -> str:
    actual = value or uuid4().hex
    if SAFE_ID.fullmatch(actual) is None:
        raise ValueError("run_id must use 1-101 letters, numbers, underscores or hyphens.")
    return actual


@contextmanager
def _exclusive(run_id: str) -> Iterator[None]:
    with _active_lock:
        if run_id in _active:
            raise ValueError(f"Run {run_id} is already active.")
        _active.add(run_id)
    try:
        yield
    finally:
        with _active_lock:
            _active.remove(run_id)


def _write_summary(directory: Path, summary: dict[str, Any]) -> dict[str, Any]:
    path = directory / "mcp_summary.json"
    path.write_text(json_text(summary) + "\n", encoding="utf-8")
    return summary | {"summary_path": str(path)}


def _job_key(root: Path, run_id: str) -> tuple[str, str]:
    return str(root.resolve()), run_id


def _optimization(
    root: Path, spec: str, *, budget: int, timeout_s: float, run_id: str | None,
    netlist_path: str | None, intake: IntakeMode, objective: Objective | None,
) -> dict[str, Any]:
    actual_run_id = _run_id(run_id)
    with _exclusive(actual_run_id):
        defaults = load_targets("easy")
        request, pending_goal = route_request(spec, defaults, intake)
        targets = request.targets if request else defaults
        task_id = "spec_" + hashlib.sha256(spec.encode("utf-8")).hexdigest()[:12]
        circuit, circuit_sha = None, ""
        if netlist_path is not None:
            loaded = load_circuit(Path(netlist_path))
            circuit, circuit_sha = loaded.body, loaded.sha

        directory = root / actual_run_id
        directory.mkdir(parents=True, exist_ok=True)
        immutable = {
            "kind": "optimize", "spec": spec, "intake": intake,
            "objective_override": objective, "circuit_sha": circuit_sha,
        }
        request_path = directory / "request.json"
        if request_path.exists():
            saved = json.loads(request_path.read_text(encoding="utf-8"))
            if saved != immutable:
                raise ValueError("Cannot reuse a run_id with a different specification or circuit.")
        else:
            request_path.write_text(json_text(immutable) + "\n", encoding="utf-8")

        summary_path = directory / "mcp_summary.json"
        if summary_path.is_file():
            completed = json.loads(summary_path.read_text(encoding="utf-8"))
            if completed.get("budget") != budget:
                raise ValueError(
                    f"Run ID {actual_run_id!r} already completed with budget "
                    f"{completed.get('budget')}; completed run IDs are immutable. "
                    f"Retry budget {budget} with a new run_id."
                )
            if completed.get("timeout_s") != timeout_s:
                raise ValueError(
                    f"Run ID {actual_run_id!r} already completed with timeout "
                    f"{completed.get('timeout_s')}; completed run IDs are immutable. "
                    f"Retry timeout {timeout_s} with a new run_id."
                )
            return completed | {
                "summary_path": str(summary_path),
            }

        LLMConfig.from_env()
        memory = Memory(directory / "memory.sqlite3")
        tracer = Tracer(task_id, actual_run_id, directory)
        planner = Planner.from_env(tracer)
        orchestrator = Orchestrator(
            task_id, actual_run_id, targets=targets, memory=memory, tracer=tracer,
            simulator=CachedSimulator(memory, tracer), planner=planner,
            yes=True, circuit=circuit, circuit_sha=circuit_sha,
            objective=objective or (request.objective if request else "feasibility"),
            assumptions=request.assumptions if request else None,
            budget=budget, timeout_s=timeout_s,
            goal_to_interpret=pending_goal, objective_override=objective,
            export_root=directory / "exports",
        )
        result = orchestrator.run()
        summary = {
            "kind": "optimize",
            "run_id": actual_run_id,
            "task_id": task_id,
            "status": result.outcome,
            "verified_success": result.outcome == "pass",
            "reason": result.message,
            "targets": orchestrator.targets.model_dump(mode="json"),
            "objective": orchestrator.objective,
            "simulations_used": result.simulations_used,
            "cache_hits": result.cache_hits,
            "budget": budget,
            "timeout_s": timeout_s,
            "design_id": result.design_id,
            "engineer_verdict": orchestrator.verdict,
            "approval_source": "automatic_mcp_tool",
            "run_id_state": "completed_immutable",
            "retry_requires_new_run_id": result.outcome != "pass",
            "next_action": (
                "Use a new run_id to retry with a different budget or timeout."
                if result.outcome != "pass"
                else "Use get_run_result to retrieve this completed result."
            ),
            "delivery": orchestrator.export_summary,
            "trace": str(tracer.path),
            "run_directory": str(directory),
        }
        return _write_summary(directory, summary)


def _run_optimization_job(
    root: Path, spec: str, *, budget: int, timeout_s: float, run_id: str,
    netlist_path: str | None, intake: IntakeMode, objective: Objective | None,
) -> None:
    key = _job_key(root, run_id)
    try:
        _optimization(
            root, spec, budget=budget, timeout_s=timeout_s, run_id=run_id,
            netlist_path=netlist_path, intake=intake, objective=objective,
        )
    except Exception as error:
        directory = root / run_id
        directory.mkdir(parents=True, exist_ok=True)
        if not (directory / "mcp_summary.json").is_file():
            _write_summary(directory, {
                "kind": "optimize",
                "run_id": run_id,
                "status": "error",
                "verified_success": False,
                "reason": str(error),
                "budget": budget,
                "timeout_s": timeout_s,
                "run_id_state": "completed_immutable",
                "retry_requires_new_run_id": True,
                "next_action": "Correct the error and retry with a new run_id.",
                "run_directory": str(directory),
            })
    finally:
        with _jobs_lock:
            _jobs.pop(key, None)


def _launch_optimization(
    root: Path, spec: str, *, budget: int, timeout_s: float, run_id: str | None,
    netlist_path: str | None, intake: IntakeMode, objective: Objective | None,
) -> dict[str, Any]:
    actual_run_id = _run_id(run_id)
    directory = root / actual_run_id
    if (directory / "mcp_summary.json").is_file():
        return _optimization(
            root, spec, budget=budget, timeout_s=timeout_s, run_id=actual_run_id,
            netlist_path=netlist_path, intake=intake, objective=objective,
        )

    key = _job_key(root, actual_run_id)
    launch_identity = {
        "spec": spec,
        "budget": budget,
        "timeout_s": timeout_s,
        "netlist_path": netlist_path,
        "intake": intake,
        "objective": objective,
    }
    with _jobs_lock:
        existing = _jobs.get(key)
        if existing is not None and existing[0].is_alive():
            if existing[1] != launch_identity:
                raise ValueError(
                    f"Run {actual_run_id} is already active with different inputs."
                )
        else:
            worker = Thread(
                target=_run_optimization_job,
                kwargs={
                    "root": root, "spec": spec, "budget": budget,
                    "timeout_s": timeout_s, "run_id": actual_run_id,
                    "netlist_path": netlist_path, "intake": intake,
                    "objective": objective,
                },
                name=f"mcp-optimize-{actual_run_id}",
                daemon=True,
            )
            _jobs[key] = (worker, launch_identity)
            worker.start()

    return {
        "kind": "optimize",
        "run_id": actual_run_id,
        "status": "running",
        "verified_success": False,
        "budget": budget,
        "timeout_s": timeout_s,
        "run_directory": str(directory),
        "next_action": (
            f"Poll get_run_result with run_id {actual_run_id!r} until status is no longer "
            "'running'."
        ),
    }


def _diagnostic(
    root: Path, mode: Literal["debug", "explain"], *, run_id: str | None,
    netlist_path: str | None, candidate: Candidate | None, budget: int,
    step_limit: int, planted_fault: bool = False, clipping_fault: bool = False,
) -> dict[str, Any]:
    actual_run_id = _run_id(run_id)
    with _exclusive(actual_run_id):
        directory = root / actual_run_id
        if directory.exists():
            raise ValueError("Diagnostic run_id already exists; choose a fresh run_id.")
        result = run_diagnostic(
            mode,
            directory=directory,
            netlist=Path(netlist_path) if netlist_path is not None else None,
            candidate=candidate or DEFAULT_CANDIDATE,
            budget=budget,
            step_limit=step_limit,
            planted_fault=planted_fault,
            clipping_fault=clipping_fault,
        )
        summary = {
            "kind": mode,
            "run_id": actual_run_id,
            "status": result["status"],
            "verified_success": result["verified_success"],
            "simulations_used": result["simulations_used"],
            "cache_hits": result["cache_hits"],
            "budget": result["budget"],
            "model_calls": result["model_calls"],
            "wall_clock_s": result["wall_clock_s"],
            "cause": result.get("cause"),
            "hypotheses": result.get("hypotheses", []),
            "review_text": result["review_text"],
            "scope": result["scope"],
            "waveform_report": result.get("waveform_report"),
            "trace": result["trace"],
            "result_path": str(directory / "result.json"),
            "report_path": str(directory / "report.md"),
            "run_directory": str(directory),
        }
        return _write_summary(directory, summary)


def _saved_summary(root: Path, run_id: str) -> dict[str, Any]:
    actual_run_id = _run_id(run_id)
    directory = root / actual_run_id
    path = directory / "mcp_summary.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8")) | {"summary_path": str(path)}
    with _jobs_lock:
        job = _jobs.get(_job_key(root, actual_run_id))
        if job is not None and job[0].is_alive():
            identity = job[1]
            progress: dict[str, Any] = {
                "simulations_used": 0,
                "cache_hits": 0,
                "budget": identity["budget"],
                "timeout_s": identity["timeout_s"],
            }
            database = directory / "memory.sqlite3"
            if database.is_file():
                task_id = "spec_" + hashlib.sha256(
                    identity["spec"].encode("utf-8")
                ).hexdigest()[:12]
                try:
                    used, hits = Memory(database).task_counts(task_id, actual_run_id)
                    progress.update(simulations_used=used, cache_hits=hits)
                except (OSError, sqlite3.Error) as error:
                    progress["progress_error"] = str(error)
            return {
                "kind": "optimize",
                "run_id": actual_run_id,
                "status": "running",
                "verified_success": False,
                "run_directory": str(directory),
                "next_action": "Poll get_run_result again.",
            } | progress
    if (directory / "request.json").is_file():
        return {
            "kind": "optimize",
            "run_id": actual_run_id,
            "status": "interrupted",
            "verified_success": False,
            "run_directory": str(directory),
            "next_action": "Call optimize_circuit with the identical request and run_id to resume.",
        }
    raise ValueError(f"No MCP run found for {actual_run_id}.")


def create_server(root: Path = MCP_ROOT) -> MCPServer:
    server = MCPServer(
        "analog-circuit-engineer",
        description=(
            "Simulator-grounded five-transistor OTA optimization, diagnosis and explanation. "
            "Models choose plans and prose; ngspice and deterministic code own measurements "
            "and electrical verdicts."
        ),
        instructions=(
            "Use optimize_circuit for an engineer specification, debug_circuit for measured "
            "fault diagnosis, and explain_circuit for a measured operating-point explanation. "
            "Optimization starts in the background; poll get_run_result until it is complete. "
            "All tools support only the repository's fixed five-transistor OTA adapter."
        ),
        version="1.1.0",
    )

    @server.tool(structured_output=True)
    def optimize_circuit(
        spec: Annotated[str, Field(min_length=1, max_length=2000)],
        budget: Annotated[int, Field(ge=1, le=500)] = BUDGET,
        timeout_s: Annotated[float, Field(gt=0, le=3600)] = 300,
        run_id: str | None = None,
        netlist_path: str | None = None,
        intake: IntakeMode = "auto",
        objective: Objective | None = None,
    ) -> dict[str, Any]:
        """Start fixed-OTA optimization in the background and return its run ID immediately.

        Passing designs and typical-corner survivors are automatically approved so this
        non-interactive MCP tool cannot block on terminal input. Poll get_run_result for
        progress and final measured evidence. Reuse run_id only to resume an interrupted
        checkpoint or retrieve an identical completed call. A completed run ID is immutable;
        retry a failed run with a new ID when changing budget or timeout.
        """
        return _launch_optimization(
            root, spec, budget=budget, timeout_s=timeout_s, run_id=run_id,
            netlist_path=netlist_path, intake=intake, objective=objective,
        )

    @server.tool(structured_output=True)
    def debug_circuit(
        run_id: str | None = None,
        netlist_path: str | None = None,
        candidate: Candidate | None = None,
        budget: Annotated[int, Field(ge=1, le=100)] = 6,
        step_limit: Annotated[int, Field(ge=1, le=8)] = 4,
        planted_fault: bool = False,
        clipping_fault: bool = False,
    ) -> dict[str, Any]:
        """Diagnose a measured OTA fault with bounded model-selected experiments.

        Electrical confirmation remains deterministic. planted_fault injects the M4 gate
        fault; clipping_fault injects the supported heavy-load transient scenario.
        """
        return _diagnostic(
            root, "debug", run_id=run_id, netlist_path=netlist_path,
            candidate=candidate, budget=budget, step_limit=step_limit,
            planted_fault=planted_fault, clipping_fault=clipping_fault,
        )

    @server.tool(structured_output=True)
    def explain_circuit(
        run_id: str | None = None,
        netlist_path: str | None = None,
        candidate: Candidate | None = None,
        budget: Annotated[int, Field(ge=1, le=100)] = 2,
    ) -> dict[str, Any]:
        """Explain the OTA using measured DC facts and code-validated numeric grounding."""
        return _diagnostic(
            root, "explain", run_id=run_id, netlist_path=netlist_path,
            candidate=candidate, budget=budget, step_limit=1,
        )

    @server.tool(structured_output=True)
    def get_run_result(run_id: str) -> dict[str, Any]:
        """Read running, interrupted, error or completed MCP state by safe run identifier."""
        return _saved_summary(root, run_id)

    return server


mcp = create_server()


def main() -> None:
    MCP_ROOT.mkdir(parents=True, exist_ok=True)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
