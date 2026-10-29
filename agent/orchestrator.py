"""Task execution with simulation budgets, engineer review and checkpoints."""

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import argparse
import hashlib
import json
import math
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent.context import PlannerState, ResultRow, format_candidate, format_target, round_line
from agent.export import export_design
from agent.memory import (
    BudgetExhausted, CachedSimulation, CachedSimulator, Memory, WorkingState,
)
from agent.netlist import load_circuit
from agent.planner import CANDIDATES_PER_ROUND, ConfigError, LLMConfig, Planner
from agent.predictor import predict, prediction_error
from agent.sim import Candidate, Condition, Metrics, SimulationResult
from agent.smoke import ROOT
from agent.spec import Objective, Request, parse_request
from agent.tradeoffs import Option, describe_options, measured_options, objective_key
from agent.tools import (
    ask_engineer, ask_pause, calc, make_candidates, quick_check, retrieve_designs, snap,
)
from agent.tracer import TraceEvent, Tracer, json_text
from agent.verifier import Targets, Verification, verify, verify_dc


SPECS_PATH = ROOT / "tasks" / "specs.yaml"
TYPICAL = Condition(name="typical", temperature_c=27, supply_v=1.8)
CORNERS = (
    TYPICAL,
    Condition(name="hot_low_supply", temperature_c=125, supply_v=1.62),
    Condition(name="cold_low_supply", temperature_c=-40, supply_v=1.62),
    Condition(name="hot_high_supply", temperature_c=125, supply_v=1.98),
    Condition(name="cold_high_supply", temperature_c=-40, supply_v=1.98),
)
START_ID = "r0c1"
BUDGET = 40
MAX_ATTEMPTS = 2
WORKERS = 4
MAX_ROUNDS = 20
STALL_ROUNDS = 3
START_SPREAD = 0.5
LOAD_F = 1e-12  # Cload in circuits/ota5t.cir.j2


def load_targets(task_id: str, path: Path = SPECS_PATH) -> Targets:
    tasks = json.loads(path.read_text(encoding="utf-8"))["tasks"]
    for task in tasks:
        if task["id"] == task_id:
            return Targets.model_validate(task["targets"])
    known = ", ".join(task["id"] for task in tasks)
    raise ValueError(f"Unknown task '{task_id}'. Known tasks: {known}.")


def size_start(
    targets: Targets, *, calculator: Callable[[str], float] = calc,
) -> tuple[Candidate, list[str]]:
    peak_supply = max(condition.supply_v for condition in CORNERS)
    gm1 = calculator(f"2 * pi * {targets.ugf_hz} * {LOAD_F}")
    vov = 0.2
    tail_ua = snap("I_bias", min(
        calculator(f"{targets.power_w} / {peak_supply} * 1e6"),
        calculator(f"{gm1} * {vov} * 1e6"),
    ))
    model = (ROOT / "models" / "ptm180.lib").read_text()
    coefficients = []
    for polarity in ("NMOS", "PMOS"):
        block = re.search(rf"\.model\s+{polarity}\s+{polarity}(.*?)(?=\.model|\Z)",
                          model, re.I | re.S)
        if block is None:
            raise ValueError(f"Missing {polarity} model for equation sizing.")
        values = {}
        for name in ("U0", "Tox"):
            match = re.search(rf"\b{name}\s*=\s*([0-9.eE+-]+)", block[1], re.I)
            if match is None or float(match[1]) <= 0:
                raise ValueError(f"Missing positive {name} in the {polarity} model.")
            values[name] = float(match[1])
        coefficients.append(calculator(f"{values['U0']} * 3.453e-11 / {values['Tox']}"))
    kn, kp = coefficients
    current = tail_ua * 1e-6
    candidate = Candidate(
        W_in=snap("W_in", calculator(f"{current} * 0.5 / ({kn} * {vov} * {vov})")), L_in=0.5,
        W_load=snap("W_load", calculator(f"{current} * 0.5 / ({kp} * {vov} * {vov})")), L_load=0.5,
        W_tail=snap("W_tail", calculator(f"2 * {current} * 0.5 / ({kn} * 0.15 * 0.15)")), L_tail=0.5,
        I_bias=tail_ua,
    )
    assumptions = [
        f"Tail current {tail_ua:g} uA: minimum of power/peak supply and gm*{vov:g} V, "
        "snapped to legal ranges; an estimate, not a measured result.",
        f"gm target {gm1:g} S = 2*pi*UGF*1 pF; assumed input/load overdrive {vov:g} V.",
        f"Square-law W/L estimates use model U0 and Tox: kn={kn:g}, kp={kp:g} A/V^2; "
        "length 0.5 um and tail overdrive 0.15 V are explicit assumptions.",
        "All five conditions must meet the same targets.",
    ]
    return candidate, assumptions


def row_key(row: ResultRow) -> tuple[bool, float]:
    return (row.passed, row.worst_margin())


def metrics_text(metrics: Metrics) -> str:
    return (
        f"gain {metrics.gain_db:.1f} dB, UGF {metrics.ugf_hz / 1e6:.1f} MHz, "
        f"PM {metrics.pm_deg:.1f} deg, power {metrics.power_w * 1e6:.1f} uW"
    )


@dataclass(frozen=True)
class RunResult:
    outcome: str
    message: str
    simulations_used: int
    cache_hits: int
    design_id: int | None


class Orchestrator:
    def __init__(
        self, task_id: str, run_id: str, *, targets: Targets, memory: Memory,
        tracer: Tracer, simulator: CachedSimulator, planner: Planner,
        budget: int = BUDGET, yes: bool = False, read: Callable[[str], str] = input,
        circuit: str | None = None, circuit_sha: str = "",
        exporter: Callable[[int, Path], dict[str, Any]] | None = None,
        export_root: Path | None = None,
        objective: Objective = "feasibility", assumptions: list[str] | None = None,
        timeout_s: float = 300,
        initial_candidates: list[Candidate] | None = None,
        use_lessons: bool = True, use_predictor: bool = True,
        warm_start: bool = False,
        candidate_prior: Candidate | None = None,
        goal_to_interpret: str | None = None,
        objective_override: Objective | None = None,
    ) -> None:
        self.task_id = task_id
        self.run_id = run_id
        self.targets = targets
        self.memory = memory
        self.tracer = tracer
        self.simulator = simulator
        self.planner = planner
        self.budget = budget
        self.yes = yes
        self.read = read
        self.circuit = circuit
        self.circuit_sha = circuit_sha
        if budget < 1 or timeout_s <= 0:
            raise ValueError("Budget and task timeout must be positive.")
        self.objective = objective
        self.request_assumptions = assumptions or []
        self.timeout_s = timeout_s
        self.initial_candidates = initial_candidates
        self.use_lessons, self.use_predictor = use_lessons, use_predictor
        self.warm_start = warm_start or candidate_prior is not None
        self.candidate_prior = candidate_prior
        self.goal_to_interpret, self.objective_override = goal_to_interpret, objective_override
        self.resolved_request: Request | None = None
        self.deadline = float("inf")
        self.exporter = exporter if exporter is not None else (
            lambda design_id, out_dir: export_design(
                design_id, out_dir, memory=memory, simulator=self._export_simulation,
            )
        )
        self.export_root = export_root if export_root is not None else ROOT / "runs" / "exports"
        self.verdict = "not_requested"
        self.design_id: int | None = None
        self.export_summary: dict[str, Any] | None = None

    def run(self) -> RunResult:
        from agent.intake import ClarificationNeeded
        start = time.perf_counter()
        self.deadline = start + self.timeout_s
        self.tracer.emit(TraceEvent(type="task_start", inputs={
            "task_id": self.task_id, "run_id": self.run_id, "budget": self.budget,
            "circuit_sha": self.circuit_sha,
            "objective": self.objective, "timeout_s": self.timeout_s,
        }))
        outcome, message = "error", ""
        try:
            outcome = self._execute()
        except ConfigError as error:
            outcome, message = "config_error", str(error)
        except ClarificationNeeded as error:
            outcome, message = "clarification_required", str(error)
        except TimeoutError as error:
            outcome, message = "deadline_exceeded", str(error)
        except (OSError, sqlite3.Error, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            outcome, message = "error", str(error)
        if outcome in ("error", "config_error", "deadline_exceeded"):
            try:
                self.tracer.tool("write_failure_lesson", self.memory.store_lesson, self.task_id,
                    f"Run {self.run_id} ended {outcome}; no reusable circuit inferred. "
                    "Inspect the trace and resume the checkpoint only after resolving the error.")
            except (OSError, sqlite3.Error, ValueError) as error:
                message += f"; failed to persist failure lesson: {error}"
        used, hits = self.memory.task_counts(self.task_id, self.run_id)
        wall_s = time.perf_counter() - start
        self.tracer.emit(TraceEvent(type="task_end", duration_ms=wall_s * 1000, outputs={
            "outcome": outcome, "simulations_used": used, "wall_clock_s": wall_s,
            "cache_hits": hits, "engineer_verdict": self.verdict,
            "approval_source": "automatic" if self.yes else "human",
            "objective": self.objective, "delivery": self.export_summary,
            "error": message,
        }))
        return RunResult(outcome, message, used, hits, self.design_id)

    def _execute(self) -> str:
        if self.goal_to_interpret is not None:
            from agent.intake import interpret_request
            from agent.reasoning import Reasoner
            from agent.spec import Request
            checkpoint = self.memory.load_checkpoint(self.task_id, self.run_id)
            if checkpoint is None:
                request = self.tracer.tool(
                    "interpret_request", interpret_request, self.goal_to_interpret,
                    self.targets, Reasoner(self.tracer),
                )
                if not self.yes:
                    summary = {"objective": self.objective_override or request.objective,
                               "targets_SI": request.targets.model_dump(),
                               "assumptions": request.assumptions}
                    answer = self.read(f"Parsed specification: {json_text(summary)}\nApprove [y/N]: ")
                    if answer.strip().lower() not in ("y", "yes"):
                        return "stopped_by_engineer"
            else:
                if checkpoint.state.execution.get("source_goal") != self.goal_to_interpret:
                    raise ValueError("Cannot resume with different source text.")
                request = Request.model_validate(checkpoint.state.execution.get("resolved_request"))
            self.targets = request.targets
            self.objective = self.objective_override or request.objective
            self.request_assumptions = request.assumptions
            self.resolved_request = request
        self.tracer.tool("read_request", Targets.model_validate, self.targets.model_dump())
        state = self._load_or_start()
        if state.step == "done":
            self.verdict = str(state.execution["verdict"])
            design = state.execution["design_id"]
            self.design_id = design if isinstance(design, int) else None
            return str(state.execution["outcome"])
        while True:
            self._check_deadline()
            winner = self._winner(state)
            if winner is not None and self.objective == "feasibility":
                return self._review(state, winner)
            if state.execution["phase"] == "evaluated":
                reason = self._stop_reason(state)
                if reason is not None:
                    return self._review(state, winner) if winner is not None else self._finish(state, reason)
                self._propose(state)
            if self._simulate(state):
                return self._finish(state, "stopped_by_engineer")
            self._evaluate(state)

    def _load_or_start(self) -> WorkingState:
        checkpoint = self.memory.load_checkpoint(self.task_id, self.run_id)
        provenance = self.tracer.tool("provenance", self.simulator.provenance)
        if checkpoint is not None:
            if checkpoint.state.targets != self.targets:
                raise ValueError(
                    f"Run {self.run_id} was started with different targets; use a new run id."
                )
            if checkpoint.state.circuit_sha != self.circuit_sha:
                raise ValueError(
                    f"Run {self.run_id} was started with a different netlist; use a new run id."
                )
            if checkpoint.state.execution.get("objective", "feasibility") != self.objective:
                raise ValueError("Cannot resume with a different objective; use a new run id.")
            if checkpoint.state.execution.get("provenance") != provenance:
                raise ValueError("Model, simulator, measurements or verifier changed; use a new run id.")
            return checkpoint.state
        self.tracer.emit(TraceEvent(type="tool_call", inputs={"tool": "select_structure"},
            outputs={"topology": "ota5t", "reason": "Only implemented circuit family."}))
        if self.initial_candidates is None:
            start, assumptions = self.tracer.tool(
                "size_start", size_start, self.targets,
                calculator=lambda expression: self.tracer.tool("calc", calc, expression),
            )
            candidates = [start, *self.tracer.tool("make_candidates", make_candidates,
                start, CANDIDATES_PER_ROUND - 1, START_SPREAD, seed=0, exclude=[start],
            )]
        else:
            candidates = self.initial_candidates
            assumptions = ["Initial population is supplied by the seeded random baseline.",
                           "All five conditions must meet the same targets."]
        records = self._lesson_records(provenance) if self.use_lessons else []
        reusable = [record for record in records if record["candidate_json"] is not None]
        if reusable:
            prior = Candidate.model_validate_json(reusable[-1]["candidate_json"])
            candidates = [prior, *[item for item in candidates if item != prior]][:CANDIDATES_PER_ROUND]
            assumptions.append(f"First candidate reuses held-out-validated lesson {reusable[-1]['id']}.")
        if self.candidate_prior is not None:
            candidates = [self.candidate_prior, *[
                item for item in candidates if item != self.candidate_prior]][:CANDIDATES_PER_ROUND]
            assumptions.append("First candidate is a controlled, held-out-validated candidate prior; "
                               "its previous verdict is not reused.")
        problems = [reason for check in self.tracer.tool("quick_check", quick_check, candidates)
                    for reason in check.reasons]
        if problems:
            raise ValueError("Start candidates failed the range check: " + "; ".join(problems))
        ids = [f"r0c{index}" for index in range(1, len(candidates) + 1)]
        state = WorkingState(
            step="start", targets=self.targets, circuit_sha=self.circuit_sha,
            assumptions=[*self.request_assumptions, *assumptions],
            candidates=dict(zip(ids, candidates)),
            execution={
                "round": 0, "phase": "proposed", "round_ids": ids,
                "hypothesis": ("Seeded random population." if self.initial_candidates is not None
                               else "Equation-sized start plus legal variants."),
                "no_improvement": 0, "exhausted": False,
                "objective": self.objective, "provenance": provenance,
                "warm_start_id": ids[0] if reusable or self.warm_start else None,
                "source_goal": self.goal_to_interpret,
                "resolved_request": self.resolved_request.model_dump(mode="json")
                if self.resolved_request else None,
            },
        )
        self._checkpoint(state)
        return state

    def _propose(self, state: WorkingState) -> None:
        round_index = int(state.execution["round"]) + 1
        planner_state = self._planner_state(state)
        base = self._base(state, planner_state)
        plan = self.planner.propose(
            planner_state, base=base, tried=list(state.candidates.values()),
            round_index=round_index,
        )
        ids = [f"r{round_index}c{index}" for index in range(1, len(plan.reply.candidates) + 1)]
        state.candidates.update(zip(ids, plan.reply.candidates))
        observations = [
            (row.candidate, row.metrics) for row in self._rows(state)
            if row.condition == TYPICAL.name and row.metrics is not None and self.use_predictor
        ]
        predictions = {}
        for cid in ids:
            estimate = self.tracer.tool("predict_candidate", predict,
                                       state.candidates[cid], observations)
            if estimate is not None:
                predictions[cid] = estimate.model_dump()
        state.execution.setdefault("predictions", {}).update(predictions)
        def rank(cid: str) -> float:
            estimate = predictions.get(cid)
            if estimate is None:
                return -1e9
            margins = [(target - estimate[name]) / target if name == "power_w"
                       else (estimate[name] - target) / target
                       for name, target in self.targets.model_dump().items()]
            if min(margins) < 0:
                return min(margins)
            if self.objective == "minimize_power":
                return 1 + self.targets.power_w / estimate["power_w"]
            if self.objective == "maximize_gain":
                return 1 + estimate["gain_db"] / self.targets.gain_db
            if self.objective == "maximize_bandwidth":
                return 1 + estimate["ugf_hz"] / self.targets.ugf_hz
            return 1 + min(margins)
        ids.sort(key=rank, reverse=True)
        state.execution.update(
            round=round_index, phase="proposed", round_ids=ids,
            hypothesis=plan.reply.hypothesis,
        )
        state.step = f"round {round_index} proposed"
        state.decisions.append(
            f"round {round_index}: source {plan.source}, {len(plan.errors)} rejected replies"
        )
        self._checkpoint(state)

    def _simulate(self, state: WorkingState) -> bool:
        """Run the round; return True if the engineer chose stop at a pause."""
        round_ids = list(state.execution["round_ids"])
        state.execution["round_started"] = time.time()
        legal = self.tracer.tool("quick_check", quick_check,
                                 [state.candidates[cid] for cid in round_ids])
        round_ids = [cid for cid, check in zip(round_ids, legal) if check.passed]
        prior = state.execution.get("warm_start_id") if state.execution["round"] == 0 else None
        if isinstance(prior, str) and prior in round_ids:
            for corner in CORNERS:
                if self._run_jobs(state, [(prior, corner)], analysis="op"):
                    break
                if not self._passes_dc(state, prior, corner):
                    break
                if self._run_jobs(state, [(prior, corner)]) or not self._passes(state, prior, corner):
                    break
                if corner == TYPICAL:
                    decisions = state.execution.setdefault("decisions", {})
                    if prior not in decisions:
                        decisions[prior] = self._vet(state, prior)
                        self._checkpoint(state)
                    if decisions[prior] == "stop":
                        return True
                    if decisions[prior] == "drop":
                        break
        exhausted = self._run_jobs(state, [(cid, TYPICAL) for cid in round_ids], analysis="op")
        passing_dc = [cid for cid in round_ids if self._passes_dc(state, cid, TYPICAL)]
        if not exhausted:
            exhausted = self._run_jobs(state, [(cid, TYPICAL) for cid in passing_dc])
        if not exhausted:
            decisions = state.execution.setdefault("decisions", {})
            for cid in passing_dc:
                if cid in decisions or not self._passes(state, cid, TYPICAL):
                    continue
                action = self._vet(state, cid)
                if action == "stop":
                    return True
                decisions[cid] = action
                self._checkpoint(state)
            passing = [
                cid for cid in passing_dc
                if self._passes(state, cid, TYPICAL) and decisions.get(cid) != "drop"
            ]
            def priority(cid: str) -> float:
                metrics = state.results[f"{cid}|{TYPICAL.name}"].metrics
                assert metrics is not None
                if self.objective == "minimize_power":
                    return -metrics.power_w
                if self.objective == "maximize_gain":
                    return metrics.gain_db
                if self.objective == "maximize_bandwidth":
                    return metrics.ugf_hz
                return min(verify(state.candidates[cid],
                                  state.results[f"{cid}|{TYPICAL.name}"], self.targets).margins.values())
            passing.sort(key=priority, reverse=True)
            complete = {option.candidate_id for option in
                        self.tracer.tool("verified_options", self._options, state)}
            selected = [cid for cid in passing if cid in complete][:3]
            deferred = [cid for cid in passing if cid not in complete]
            capacity = max(0, self._remaining() // (2 * (len(CORNERS) - 1)))
            admitted = min(3 - len(selected), capacity)
            selected += deferred[:admitted]
            deferred = deferred[admitted:]
            batch = 0
            while selected and not exhausted:
                self.tracer.emit(TraceEvent(type="tool_call", inputs={"tool": "select_survivors"},
                    outputs={"selected": selected, "deferred": deferred, "batch": batch,
                             "condition_order": [corner.name for corner in CORNERS[1:]]}))
                for corner in CORNERS[1:]:
                    if not selected or exhausted:
                        break
                    exhausted = self._run_jobs(
                        state, [(cid, corner) for cid in selected], analysis="op",
                    )
                    selected = [cid for cid in selected if self._passes_dc(state, cid, corner)]
                    if not exhausted:
                        exhausted = self._run_jobs(state, [(cid, corner) for cid in selected])
                    selected = [cid for cid in selected if self._passes(state, cid, corner)]
                if selected or exhausted or not deferred:
                    break
                capacity = max(0, self._remaining() // (2 * (len(CORNERS) - 1)))
                if not capacity:
                    exhausted = True
                    break
                admitted = min(3, capacity)
                selected = deferred[:admitted]
                deferred = deferred[admitted:]
                batch += 1
            if passing and not selected and deferred and not capacity:
                exhausted = True
        state.execution["exhausted"] = exhausted
        return False

    def _vet(self, state: WorkingState, cid: str) -> str:
        result = state.results[f"{cid}|{TYPICAL.name}"]
        assert result.metrics is not None
        question = "\n".join([
            f"Pause: candidate {cid} passes typical: {format_candidate(state.candidates[cid])}",
            f"  {metrics_text(result.metrics)}",
            "  keep: run the other corners | drop: exclude it | "
            "pin: run the other corners and use it as the next base | stop: end the run now",
        ])
        return self.tracer.tool("ask_pause", ask_pause, question, yes=self.yes, read=self.read)

    def _base(self, state: WorkingState, planner_state: PlannerState) -> Candidate:
        decisions = state.execution.get("decisions", {})
        pinned = [row for row in self._rows(state) if decisions.get(row.candidate_id) == "pin"]
        if pinned:
            return max(pinned, key=row_key).candidate
        if planner_state.best is not None:
            return planner_state.best.candidate
        for cid, candidate in state.candidates.items():
            if decisions.get(cid) != "drop":
                return candidate
        return state.candidates[START_ID]

    def _run_jobs(
        self, state: WorkingState, jobs: list[tuple[str, Condition]], *, analysis: str = "ac",
    ) -> bool:
        """Run pending jobs in parallel; return True if the budget ran out."""
        pending: dict[Future[CachedSimulation | None], tuple[str, str]] = {}
        pool = ThreadPoolExecutor(max_workers=WORKERS)
        exhausted = False
        failure: Exception | None = None
        try:
            for cid, condition in jobs:
                self._check_deadline()
                suffix = "|op" if analysis == "op" else ""
                key = f"{cid}|{condition.name}{suffix}"
                job_id = f"{cid}-{condition.name}-{analysis}"
                if key in state.results:
                    continue
                saved = self.memory.load_job(self.task_id, self.run_id, job_id)
                if saved is not None and saved[2]:
                    state.results[key] = saved[0]
                    continue
                future = pool.submit(self._simulate_job, state.candidates[cid], condition,
                                     job_id, analysis, time.perf_counter())
                pending[future] = (key, job_id)
            for future in as_completed(pending):
                key, job_id = pending[future]
                try:
                    answer = future.result()
                except Exception as error:
                    failure = failure or error
                    continue
                if answer is None:
                    if not exhausted:
                        print("  Budget reached; no more simulations.", flush=True)
                    exhausted = True
                    continue
                state.results[key] = answer.result
                self.memory.save_job(
                    self.task_id, self.run_id, job_id, answer.result,
                    cache_hit=answer.cache_hit, done=True,
                )
                self._report(state, key, answer)
        finally:
            pool.shutdown(wait=True)
        if failure is not None:
            raise failure
        return exhausted

    def _simulate_job(
        self, candidate: Candidate, condition: Condition, job_id: str,
        analysis: str = "ac", queued_at: float | None = None,
    ) -> CachedSimulation | None:
        def reserve() -> None:
            self._check_deadline()
            self.memory.reserve_simulation(
                self.task_id, self.run_id, job_id, self.budget, MAX_ATTEMPTS,
            )

        try:
            self._check_deadline()
            return self.simulator.simulate(
                candidate, condition, before_run=reserve, circuit=self.circuit,
                analysis="op" if analysis == "op" else "ac",
                timeout_s=min(30, self.deadline - time.perf_counter()),
                queue_wait_ms=(time.perf_counter() - queued_at) * 1000 if queued_at else 0,
            )
        except BudgetExhausted:
            return None

    def _report(self, state: WorkingState, key: str, answer: CachedSimulation) -> None:
        cid, corner = key.split("|")[:2]
        result = answer.result
        estimate = state.execution.get("predictions", {}).get(cid)
        if estimate is not None and result.analysis == "ac" and corner == TYPICAL.name and result.metrics:
            errors = prediction_error(Metrics.model_validate(estimate), result.metrics, self.targets)
            self.tracer.emit(TraceEvent(type="tool_call", inputs={
                "tool": "prediction_error", "candidate_id": cid,
            }, outputs={"label": "estimate_not_measurement", "predicted": estimate,
                        "measured": result.metrics.model_dump(), "normalized_absolute_error": errors}))
        cache = " (cache)" if answer.cache_hit else ""
        if result.metrics is None:
            verdict = self.tracer.tool("verify_dc", verify_dc, state.candidates[cid],
                                      result, self.targets)
            print(f"  {cid} {corner} DC: {'PASS' if verdict.passed else 'FAIL'}{cache}",
                  flush=True)
            return
        status = "PASS" if self.tracer.tool(
            "verify", verify, state.candidates[cid], result, self.targets,
        ).passed else "FAIL"
        print(f"  {cid} {corner}: {status} {metrics_text(result.metrics)}{cache}", flush=True)

    def _evaluate(self, state: WorkingState) -> None:
        round_index = int(state.execution["round"])
        round_ids = set(state.execution["round_ids"])
        rows = self._rows(state)
        this_round = [
            row for row in self._feedback_rows(state) if row.candidate_id in round_ids
        ]
        earlier = [row_key(row) for row in rows if row.candidate_id not in round_ids]
        best = max(rows, key=row_key, default=None)
        improved = best is not None and (not earlier or row_key(best) > max(earlier))
        options = self._options(state) if self.objective != "feasibility" else []
        if options:
            previous = [option for option in options if option.candidate_id not in round_ids]
            improved = not previous or objective_key(options[0], self.objective) > max(
                objective_key(option, self.objective) for option in previous)
        no_improvement = 0 if improved else int(state.execution["no_improvement"]) + 1
        state.round_summaries.append(
            round_line(round_index, str(state.execution["hypothesis"]), this_round)
        )
        state.best_candidate_id = options[0].candidate_id if options else (
            best.candidate_id if best is not None else None)
        state.execution.update(phase="evaluated", no_improvement=no_improvement)
        state.step = f"round {round_index} evaluated"
        self.tracer.emit(TraceEvent(type="round_summary", outputs={
            "round": round_index, "simulated": len(this_round),
            "condition_results": len(this_round),
            "passed": sum(row.passed for row in this_round),
            "no_improvement_rounds": no_improvement,
        }, duration_ms=max(0, (time.time() - float(state.execution["round_started"])) * 1000)))
        self._checkpoint(state)

    def _stop_reason(self, state: WorkingState) -> str | None:
        if state.execution["exhausted"] or self._remaining() <= 0:
            return "budget_exhausted"
        if int(state.execution["no_improvement"]) >= STALL_ROUNDS:
            return "stalled"
        if int(state.execution["round"]) >= MAX_ROUNDS:
            return "round_limit"
        return None

    def _review(self, state: WorkingState, winner: str) -> str:
        candidate = state.candidates[winner]
        results = [state.results[f"{winner}|{corner.name}"] for corner in CORNERS]
        verdict = self.tracer.tool("ask_engineer", ask_engineer,
            self._review_text(winner, state, results), yes=self.yes, read=self.read,
        )
        self.verdict = verdict
        if verdict == "reject":
            return self._finish(state, "pass_rejected")
        existing = [
            design for design in self.memory.designs()
            if design.candidate == candidate and design.targets == self.targets
            and design.circuit_sha == self.circuit_sha
            and design.provenance.get("simulation_provenance") == state.execution["provenance"]
        ]
        if existing:
            self.design_id = existing[-1].id
        else:
            used, hits = self.memory.task_counts(self.task_id, self.run_id)
            self.design_id = self.memory.store_design(
                candidate, self.targets, results, approved=True, circuit_sha=self.circuit_sha,
                circuit_body=self.circuit or "",
                provenance={
                    "task_id": self.task_id, "run_id": self.run_id,
                    "simulations_used": used, "cache_hits": hits,
                    "simulation_provenance": state.execution["provenance"],
                },
            )
        self._export(self.design_id)
        delivered = self.export_summary or {}
        failed = "error" in delivered or (
            "round_trip" in delivered and not delivered["round_trip"]["passed"]
        )
        return self._finish(state, "delivery_failed" if failed else "pass")

    def _export(self, design_id: int) -> None:
        out_dir = self.export_root / self.run_id
        started = time.perf_counter()
        try:
            self.export_summary = self.exporter(design_id, out_dir)
            self.export_summary["folder"] = str(out_dir)
        except (OSError, RuntimeError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
            self.export_summary = {"folder": str(out_dir), "error": str(error)}
        self.tracer.emit(TraceEvent(type="export", outputs=self.export_summary,
                                   duration_ms=(time.perf_counter() - started) * 1000))

    def _export_simulation(
        self, candidate: Candidate, condition: Condition, **kwargs: Any,
    ) -> SimulationResult:
        self._check_deadline()
        job_id = f"export-{condition.name}"
        answer = self.simulator.simulate(
            candidate, condition, analysis="ac", netlist=kwargs["netlist"],
            model_contents=kwargs["model_contents"],
            timeout_s=min(30, self.deadline - time.perf_counter()),
            before_run=lambda: self.memory.reserve_simulation(
                self.task_id, self.run_id, job_id, self.budget, MAX_ATTEMPTS),
        )
        self.memory.save_job(self.task_id, self.run_id, job_id, answer.result,
                             cache_hit=answer.cache_hit, done=True)
        return answer.result

    def _review_text(
        self, winner: str, state: WorkingState, results: list[SimulationResult],
    ) -> str:
        targets = " | ".join(
            format_target(name, target) for name, target in self.targets.model_dump().items()
        )
        lines = [
            describe_options(self._options(state), self.objective),
            f"Candidate {winner} passes all five conditions: "
            f"{format_candidate(state.candidates[winner])}",
            f"targets: {targets}",
        ]
        for result in results:
            metrics = result.metrics
            if metrics is None:
                continue
            lines.append(
                f"- {result.condition.name}: gain {metrics.gain_db:.1f} dB, "
                f"UGF {metrics.ugf_hz / 1e6:.1f} MHz, PM {metrics.pm_deg:.1f} deg, "
                f"power {metrics.power_w * 1e6:.1f} uW"
            )
        lines.append("Approve to store this design in the library, or reject to discard it.")
        return "\n".join(lines)

    def _finish(self, state: WorkingState, outcome: str) -> str:
        if "lesson_id" not in state.execution:
            options = self._options(state)
            def robustness(option: Option) -> float:
                return min((target - getattr(option.worst, name)) / target if name == "power_w"
                           else (getattr(option.worst, name) - target) / target
                           for name, target in self.targets.model_dump().items())
            option = max(options, key=robustness) if options and outcome == "pass" else None
            text = (
                f"Run {self.run_id}: reuse this measured all-condition candidate only after "
                f"held-out validation: {option.candidate.model_dump_json()}. "
                f"Worst-case metrics: {option.worst.model_dump_json()}."
                if option else
                f"Run {self.run_id} ended {outcome} without an approved, successfully "
                "delivered reusable design; "
                "do not infer feasibility from partial condition results."
            )
            state.execution["lesson_id"] = self.tracer.tool(
                "write_proposed_lesson", self.memory.store_lesson, self.task_id, text,
                candidate=option.candidate if option else None,
                circuit_sha=self.circuit_sha, provenance=str(state.execution["provenance"]),
            )
        state.step = "done"
        state.execution.update(outcome=outcome, verdict=self.verdict, design_id=self.design_id)
        self._checkpoint(state)
        return outcome

    def _winner(self, state: WorkingState) -> str | None:
        options = self._options(state)
        return options[0].candidate_id if options else None

    def _options(self, state: WorkingState) -> list[Option]:
        decisions = state.execution.get("decisions", {})
        return measured_options(
            state.candidates, state.results, self.targets,
            [corner.name for corner in CORNERS], self.objective,
            {cid for cid in state.candidates if decisions.get(cid) == "drop"},
        )

    def _passes_dc(self, state: WorkingState, cid: str, condition: Condition) -> bool:
        result = state.results.get(f"{cid}|{condition.name}|op")
        return result is not None and self.tracer.tool(
            "verify_dc", verify_dc, state.candidates[cid], result, self.targets,
        ).passed

    def _passes(self, state: WorkingState, cid: str, condition: Condition) -> bool:
        result = state.results.get(f"{cid}|{condition.name}")
        return result is not None and self.tracer.tool(
            "verify", verify, state.candidates[cid], result, self.targets,
        ).passed

    def _rows(self, state: WorkingState) -> list[ResultRow]:
        decisions = state.execution.get("decisions", {})
        rows = []
        for key, result in state.results.items():
            if key.endswith("|op"):
                continue
            cid = key.split("|", 1)[0]
            if decisions.get(cid) == "drop":
                continue
            candidate = state.candidates[cid]
            rows.append(ResultRow.build(
                cid, candidate, result, self.tracer.tool("verify", verify, candidate,
                                                       result, self.targets),
            ))
        return rows

    def _feedback_rows(self, state: WorkingState) -> list[ResultRow]:
        """Return the furthest measured stage for each candidate and condition."""
        decisions = state.execution.get("decisions", {})
        selected: dict[tuple[str, str], tuple[SimulationResult, Verification]] = {}
        for key, result in state.results.items():
            cid = key.split("|", 1)[0]
            if decisions.get(cid) == "drop":
                continue
            pair = (cid, result.condition.name)
            if result.analysis == "op":
                if pair not in selected:
                    selected[pair] = (
                        result,
                        self.tracer.tool(
                        "verify_dc", verify_dc, state.candidates[cid], result, self.targets,
                        ),
                    )
            else:
                selected[pair] = (
                    result,
                    self.tracer.tool(
                        "verify", verify, state.candidates[cid], result, self.targets,
                    ),
                )
        candidate_order = {cid: index for index, cid in enumerate(state.candidates)}
        condition_order = {condition.name: index for index, condition in enumerate(CORNERS)}
        rows = [
            ResultRow.build(cid, state.candidates[cid], result, verdict)
            for (cid, _), (result, verdict) in selected.items()
        ]
        return sorted(rows, key=lambda row: (
            row.passed,
            candidate_order[row.candidate_id],
            condition_order.get(row.condition, len(condition_order)),
        ))

    def _planner_state(self, state: WorkingState) -> PlannerState:
        rows = self._rows(state)
        round_ids = set(state.execution["round_ids"])
        designs = self.tracer.tool("retrieve_designs", retrieve_designs, self.memory, self.targets,
                                  circuit_sha=self.circuit_sha,
                                  provenance=str(state.execution["provenance"]))
        best = max(rows, key=row_key, default=None)
        winner = self._winner(state) if self.objective != "feasibility" else None
        if winner is not None:
            best = min((row for row in rows if row.candidate_id == winner), key=row_key)
        return PlannerState(
            targets=self.targets,
            objective=self.objective,
            assumptions=list(state.assumptions),
            round_lines=list(state.round_summaries),
            best=best,
            latest=[
                row for row in self._feedback_rows(state)
                if row.candidate_id in round_ids
            ],
            simulations_used=self.memory.task_counts(self.task_id, self.run_id)[0],
            budget=self.budget,
            no_improvement_rounds=int(state.execution["no_improvement"]),
            all_conditions_passed=self._winner(state) is not None,
            lessons=[record["text"] for record in self._lesson_records(str(state.execution["provenance"]))]
            if self.use_lessons else [],
            design=designs[0] if designs else None,
        )

    def _remaining(self) -> int:
        return self.budget - self.memory.task_counts(self.task_id, self.run_id)[0]

    def _checkpoint(self, state: WorkingState) -> None:
        state.simulations_used, state.cache_hits = self.memory.task_counts(
            self.task_id, self.run_id,
        )
        self.memory.save_checkpoint(self.task_id, self.run_id, state)

    def _check_deadline(self) -> None:
        if time.perf_counter() >= self.deadline:
            raise TimeoutError(f"Task exceeded its {self.timeout_s:g} s execution deadline.")

    def _lesson_records(self, provenance: str) -> list[dict]:
        records = self.tracer.tool("retrieve_lessons", self.memory.lesson_records)
        return [record for record in records if record["circuit_sha"] == self.circuit_sha
                and record["provenance"] == provenance]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one circuit-sizing task.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--task", help="Task id from tasks/specs.yaml, e.g. easy.")
    source.add_argument("--spec", help="Engineer goal with targets and an optional objective.")
    parser.add_argument("--intake", choices=("auto", "deterministic", "model"), default="auto",
                        help="Spec intake: strict parser, model with source-bound quantities, or automatic fallback.")
    parser.add_argument("--objective", choices=("feasibility", "minimize_power", "maximize_gain",
                                               "maximize_bandwidth"))
    parser.add_argument("--budget", type=int, default=BUDGET)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--run-id", default=None, help="Reuse an id to resume a stopped run.")
    parser.add_argument(
        "--netlist", default=None,
        help="Engineer circuit file (devices, sources, models). Default: the built-in OTA.",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Approve passing designs and keep candidates at pauses without asking.",
    )
    args = parser.parse_args(argv)
    if args.budget < 1 or not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--budget and --timeout must be positive and finite.")
    try:
        request, pending_goal = None, None
        if args.spec:
            from agent.intake import route_request
            request, pending_goal = route_request(args.spec, load_targets("easy"), args.intake)
        targets = request.targets if request else load_targets(args.task or "easy")
        task_id = args.task or "spec_" + hashlib.sha256(args.spec.encode()).hexdigest()[:12]
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"ORCHESTRATOR FAIL: {error}", file=sys.stderr)
        return 2
    circuit, circuit_sha = None, ""
    if args.netlist is not None:
        try:
            loaded = load_circuit(Path(args.netlist))
        except (OSError, ValueError) as error:
            print(f"ORCHESTRATOR FAIL: {error}", file=sys.stderr)
            return 2
        circuit, circuit_sha = loaded.body, loaded.sha
        print(f"Netlist: {args.netlist} (sha {circuit_sha[:12]})")
    run_id = args.run_id or uuid4().hex
    try:
        LLMConfig.from_env()
    except ConfigError as error:
        print(f"ORCHESTRATOR NOT CONFIGURED: {error}", file=sys.stderr)
        return 2
    tracer = Tracer(task_id, run_id)
    memory = Memory()
    planner = Planner.from_env(tracer)
    orchestrator = Orchestrator(
        task_id, run_id, targets=targets, memory=memory, tracer=tracer,
        simulator=CachedSimulator(memory, tracer), planner=planner, yes=args.yes,
        circuit=circuit, circuit_sha=circuit_sha,
        objective=args.objective or (request.objective if request else "feasibility"),
        assumptions=request.assumptions if request else None,
        budget=args.budget, timeout_s=args.timeout,
        goal_to_interpret=pending_goal, objective_override=args.objective,
    )
    try:
        result = orchestrator.run()
    except KeyboardInterrupt:
        print(f"Interrupted. Resume with: --task {args.task} --run-id {run_id}", file=sys.stderr)
        return 130
    print(f"Outcome: {result.outcome}")
    if result.message:
        print(f"Reason: {result.message}")
    print(f"Simulations used: {result.simulations_used} of {args.budget}; cache hits: {result.cache_hits}")
    print(f"Run id: {run_id} (resume with --run-id {run_id})")
    if result.design_id is not None:
        print(f"Design stored: {result.design_id}")
        export = orchestrator.export_summary or {}
        if "error" in export:
            print(f"Export FAILED: {export['error']}")
        elif export:
            verdict = "PASS" if export["round_trip"]["passed"] else "FAIL"
            print(f"Exported: {export['folder']} (round trip {verdict})")
    print(f"Trace: {tracer.path}")
    return 0 if result.outcome == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
