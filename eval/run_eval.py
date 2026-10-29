"""Reproducible, simulator-backed comparisons of the real sizing orchestrator."""

import argparse
from collections import Counter
from collections.abc import Iterable
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import random
import sqlite3
import subprocess
import sys
import time
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.context import PlannerState
from agent.diagnostics import run_diagnostic
from agent.memory import CachedSimulator, Memory
from agent.orchestrator import BUDGET, CORNERS, WORKERS, Orchestrator
from agent.planner import (
    CANDIDATES_PER_ROUND, ChatResponse, LLMConfig, Plan, Planner, PlannerReply, chat_completion,
)
from agent.sim import Candidate, ngspice_version
from agent.smoke import ROOT, find_ngspice
from agent.spec import Objective, Request, parse_request
from agent.tools import candidate_key, candidate_ranges, snap
from agent.tracer import TraceEvent, Tracer, json_text
from agent.verifier import Targets, verify


SUITE = ROOT / "eval" / "suite.json"


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,60}$")
    mode: Literal["optimize", "debug", "explain"]
    difficulty: Literal["easy", "medium", "hard"]
    goal: str = Field(min_length=1)
    objective: Objective = "feasibility"
    spec: str | None = Field(default=None, min_length=1, max_length=2000)
    targets: Targets | None = None
    expected_feasible: bool = True
    fixture: str = ""
    fault: Literal["mirror_gate", "clipping_load"] | None = None
    expected: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_targets(self) -> "EvalCase":
        if self.mode == "optimize" and self.targets is None:
            raise ValueError("Optimization cases require explicit numerical targets.")
        if self.spec is not None:
            if self.mode != "optimize" or self.targets is None:
                raise ValueError("Only optimization cases accept a spec.")
            request = parse_request(self.spec, self.targets)
            if request.targets != self.targets or request.objective != self.objective:
                raise ValueError("Parsed specification must match the independent eval contract.")
        return self


class EvalSuite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cases: list[EvalCase] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_ids(self) -> "EvalSuite":
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("Evaluation case ids must be unique.")
        return self


def random_candidates(seed: int, tried: list[Candidate]) -> list[Candidate]:
    """Uniform random search over the same legal, snapped parameter space."""
    rng = random.Random(seed)
    seen = {candidate_key(item) for item in tried}
    candidates = []
    for _ in range(10000):
        candidate = Candidate(**{
            name: snap(name, rng.uniform(low, high))
            for name, (low, high) in candidate_ranges().items()
        })
        key = candidate_key(candidate)
        if key not in seen:
            candidates.append(candidate)
            seen.add(key)
            if len(candidates) == CANDIDATES_PER_ROUND:
                return candidates
    raise ValueError("Random baseline could not generate a distinct population.")


class RandomPlanner(Planner):
    def __init__(self, tracer: Tracer, seed: int) -> None:
        def no_model(messages: list[dict[str, str]]) -> ChatResponse:
            raise RuntimeError("The random baseline must never call a model.")

        super().__init__(no_model, tracer, "random_search")
        self.seed = seed

    def propose(
        self, state: PlannerState, *, base: Candidate,
        tried: Iterable[Candidate], round_index: int,
    ) -> Plan:
        start = time.perf_counter()
        candidates = random_candidates(self.seed + round_index * 1000003, list(tried))
        self.tracer.emit(TraceEvent(
            type="tool_call", duration_ms=(time.perf_counter() - start) * 1000,
            inputs={"tool": "random_candidates", "round": round_index, "seed": self.seed},
            outputs={"candidates": len(candidates)},
        ))
        return Plan(PlannerReply(
            hypothesis="Uniform random continuation; no model or prediction-based ranking. "
                       "Controlled candidate priors are recorded separately.",
            candidates=candidates, stop=False,
        ), "fallback", [])


def summarize_run(
    case: EvalCase, memory: Memory, tracer: Tracer, run_id: str,
) -> dict:
    """Recheck stored simulator results, rather than trusting a model or outcome string."""
    if case.targets is None:
        raise ValueError("Numerical summaries require optimization targets.")
    checkpoint = memory.load_checkpoint(case.id, run_id)
    events = [
        TraceEvent.model_validate_json(line)
        for line in tracer.path.read_text(encoding="utf-8").splitlines()
    ]
    simulations = [event for event in events if event.type == "simulation"]
    rows = []
    candidates = {}
    if checkpoint is not None:
        state = checkpoint.state
        for cid, candidate in state.candidates.items():
            results = {
                corner.name: state.results[f"{cid}|{corner.name}"]
                for corner in CORNERS if f"{cid}|{corner.name}" in state.results
            }
            if not results:
                continue
            checks = {
                name: verify(candidate, result, case.targets)
                for name, result in results.items()
            }
            complete = len(results) == len(CORNERS)
            passing = complete and all(check.passed for check in checks.values())
            valid_metrics = all(
                result.status == "ok" and result.error is None and result.metrics is not None
                for result in results.values()
            )
            robust = None
            if complete and valid_metrics:
                robust = {
                    name: (max if name == "power_w" else min)(
                        getattr(result.metrics, name) for result in results.values()
                    )
                    for name in case.targets.model_dump()
                }
            item = {
                "candidate_id": cid, "candidate": candidate.model_dump(),
                "complete": complete, "passed": passing, "worst_case_metrics": robust,
                "worst_margin": min(
                    (margin for check in checks.values() for margin in check.margins.values()),
                    default=None,
                ),
                "conditions": {
                    name: {
                        "status": result.status,
                        "metrics": result.metrics.model_dump() if result.metrics else None,
                        "bias": {device: point.model_dump() for device, point in result.bias.items()},
                        "passed": checks[name].passed, "failures": checks[name].failures,
                        "run_dir": result.run_dir,
                    }
                    for name, result in results.items()
                },
            }
            candidates[candidate_key(candidate)] = cid
            rows.append(item)
    progress = []
    observed: dict[str, set[str]] = {}
    by_id = {row["candidate_id"]: row for row in rows}
    used = hits = 0
    first_pass = None
    start = events[0].timestamp
    for event in simulations:
        used += int(not event.cache_hit)
        hits += int(bool(event.cache_hit))
        candidate = Candidate.model_validate(event.inputs["candidate"])
        cid = candidates.get(candidate_key(candidate))
        if cid is not None and event.condition is not None and event.inputs.get("analysis") == "ac":
            observed.setdefault(cid, set()).add(event.condition.name)
        completed = [
            row for key, row in by_id.items()
            if len(observed.get(key, set())) == len(CORNERS)
            and row["worst_case_metrics"] is not None
        ]
        best = max(
            (row["worst_margin"] for row in completed if row["worst_margin"] is not None),
            default=None,
        )
        if first_pass is None and any(row["passed"] for row in completed):
            first_pass = used
        progress.append({
            "simulations_used": used, "cache_hits": hits,
            "wall_clock_s": (event.timestamp - start).total_seconds(),
            "best_complete_worst_margin": best,
        })
    passing = [row for row in rows if row["passed"]]
    pareto = [
        row for row in passing
        if not any(dominates(other, row) for other in passing if other is not row)
    ]
    counts = Counter(event.type for event in events)
    return {
        "verified_success": bool(passing),
        "simulations_to_first_pass": first_pass,
        "progress": progress, "candidates": rows, "pareto_options": pareto,
        "trace_events": dict(counts),
        "model_calls": counts["model_call"],
        "token_usage": {key: sum(
            event.outputs.get("usage", {}).get(key, 0)
            for event in events if event.type == "model_call"
        ) for key in ("prompt_tokens", "completion_tokens", "total_tokens")},
        "fallback_calls": sum(
            event.type == "tool_call" and event.inputs.get("tool") == "make_candidates"
            for event in events
        ),
        "simulation_errors": sum(event.status != "ok" for event in simulations),
        "simulation_run_s": sum(event.run_ms or 0 for event in simulations) / 1000,
        "queue_wait_s": sum(event.queue_wait_ms or 0 for event in simulations) / 1000,
        "trace": str(tracer.path),
    }


def dominates(left: dict, right: dict) -> bool:
    a, b = left["worst_case_metrics"], right["worst_case_metrics"]
    if a is None or b is None:
        return False
    comparisons = [(b[name] - a[name]) if name == "power_w" else (a[name] - b[name])
                   for name in a]
    return all(value >= 0 for value in comparisons) and any(value > 0 for value in comparisons)


def run_case(
    case: EvalCase, strategy: Literal["agent", "random"], seed: int,
    budget: int, out_dir: Path,
    *, warm_candidate: Candidate | None = None, resolved_request: Request | None = None,
    resume: bool = False,
) -> dict:
    case_dir = out_dir / f"{case.id}_{strategy}_s{seed}"
    if case.mode != "optimize":
        result = run_diagnostic(
            case.mode, directory=case_dir, budget=budget,
            planted_fault=case.fault == "mirror_gate",
            clipping_fault=case.fault == "clipping_load",
        )
        result.update(
            case_id=case.id, strategy=strategy, seed=seed, goal=case.goal,
            difficulty=case.difficulty, fixture=case.fixture, expected=case.expected,
            candidates=[], pareto_options=[], progress=[],
            simulation_errors=sum(probe["result"]["status"] != "ok" for probe in result["probes"]),
        )
    else:
        case_dir.mkdir(parents=True, exist_ok=resume)
        if case.targets is None:
            raise ValueError("Optimization case is missing its targets.")
        request = resolved_request or (parse_request(case.spec, case.targets) if case.spec else None)
        if request is not None and (
            request.targets != case.targets or request.objective != case.objective
        ):
            raise ValueError("Resolved intake must match the independent evaluation targets/objective.")
        memory = Memory(case_dir / "memory.sqlite3")
        run_id = f"{strategy}_s{seed}"
        tracer = Tracer(case.id, run_id, case_dir)
        planner = Planner.from_env(tracer) if strategy == "agent" else RandomPlanner(tracer, seed)
        initial = random_candidates(seed, []) if strategy == "random" else None

        def skip_export(design_id: int, path: Path) -> dict:
            return {"skipped": True, "reason": "Export round trips are outside the search benchmark."}

        orchestrator = Orchestrator(
            case.id, run_id, targets=case.targets, memory=memory, tracer=tracer,
            simulator=CachedSimulator(memory, tracer), planner=planner,
            budget=budget, yes=True, exporter=skip_export, export_root=case_dir / "exports",
            objective=case.objective,
            initial_candidates=initial, use_lessons=False, use_predictor=strategy == "agent",
            candidate_prior=warm_candidate,
            assumptions=request.assumptions if request else None,
        )
        outcome = orchestrator.run()
        checkpoint = memory.load_checkpoint(case.id, run_id)
        review_text = "(No final engineering review was presented.)"
        reasoning = []
        if checkpoint is not None:
            winner = orchestrator._winner(checkpoint.state)
            if winner is not None:
                review_text = orchestrator._review_text(
                    winner, checkpoint.state,
                    [checkpoint.state.results[f"{winner}|{corner.name}"] for corner in CORNERS],
                )
            reasoning = [
                {"kind": "sizing_assumption", "text": text}
                for text in checkpoint.state.assumptions
            ]
        for line in tracer.path.read_text(encoding="utf-8").splitlines():
            event = TraceEvent.model_validate_json(line)
            if event.type == "model_call" and event.outputs.get("valid"):
                reasoning.append({
                    "kind": "planner_hypothesis", "round": event.inputs.get("round"),
                    "text": event.outputs.get("hypothesis"),
                })
        result = {
            "case_id": case.id, "mode": case.mode, "difficulty": case.difficulty,
            "goal": case.goal, "targets": case.targets.model_dump(),
            "input_spec": request.goal if request else case.spec,
            "requested_objective": case.objective,
            "objective_supported": True,
            "expected_feasible": case.expected_feasible,
            "strategy": strategy, "seed": seed, "budget": budget,
            "status": outcome.outcome, "error": outcome.message,
            "simulations_used": outcome.simulations_used, "cache_hits": outcome.cache_hits,
            "engineer_verdict": "automatic_eval_approval_not_human_review",
            "review_text": review_text, "reasoning_artifacts": reasoning,
            **summarize_run(case, memory, tracer, run_id),
        }
        end = next(
            TraceEvent.model_validate_json(line)
            for line in reversed(tracer.path.read_text(encoding="utf-8").splitlines())
            if json.loads(line)["type"] == "task_end"
        )
        result["wall_clock_s"] = end.outputs["wall_clock_s"]
    (case_dir / "result.json").write_text(json_text(result) + "\n", encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=SUITE)
    parser.add_argument("--strategy", choices=("agent", "random", "both"), default="both")
    parser.add_argument("--task", action="append", help="Case id; repeat to select several.")
    parser.add_argument("--seed", action="append", type=int, help="Random baseline seed; default 0,1,2.")
    parser.add_argument("--budget", type=int, default=BUDGET)
    parser.add_argument("--judge", choices=("llm", "off"), default="llm",
                        help="Qualitative LLM assessment plus hard checks (default llm).")
    parser.add_argument("--judge-model", default=None,
                        help="Optional separate Foundry deployment for judging.")
    parser.add_argument("--out", type=Path, default=None, help="New output directory; never overwritten.")
    args = parser.parse_args(argv)
    try:
        if args.budget < 1:
            raise ValueError("--budget must be positive.")
        suite = EvalSuite.model_validate_json(args.suite.read_text(encoding="utf-8"))
        selected = set(args.task or [case.id for case in suite.cases])
        unknown = selected - {case.id for case in suite.cases}
        if unknown:
            raise ValueError(f"Unknown evaluation cases: {', '.join(sorted(unknown))}")
        cases = [case for case in suite.cases if case.id in selected]
        strategies = ["agent", "random"] if args.strategy == "both" else [args.strategy]
        seeds = list(dict.fromkeys(args.seed if args.seed is not None else [0, 1, 2]))
        needs_model = args.judge == "llm" or any(case.mode != "optimize" for case in cases) or (
            "agent" in strategies and any(case.mode == "optimize" for case in cases))
        config = LLMConfig.from_env() if needs_model else None
        judge_config = config
        if judge_config is not None and args.judge_model is not None:
            if not args.judge_model.strip():
                raise ValueError("--judge-model cannot be empty.")
            judge_config = judge_config.model_copy(update={"model": args.judge_model.strip()})
        version = ngspice_version(find_ngspice())
        out_dir = args.out or ROOT / "runs" / "eval" / uuid4().hex
        out_dir.mkdir(parents=True, exist_ok=False)
        sources = [
            *sorted((ROOT / "agent").glob("*.py")), ROOT / "prompts" / "planner.md",
            ROOT / "models" / "ptm180.lib", ROOT / "circuits" / "ota5t.cir.j2",
            *sorted((ROOT / "eval").glob("*.py")), args.suite,
            ROOT / "prompts" / "eval_judge.md",
            ROOT / "eval" / "holdout.json",
        ]
        manifest = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "suite": suite.model_dump(mode="json"), "selected_cases": [case.id for case in cases],
            "strategies": strategies, "seeds": seeds, "budget": args.budget, "workers": WORKERS,
            "ngspice_version": version, "python_version": sys.version,
            "planner_model": config.model if config is not None and "agent" in strategies else None,
            "diagnostic_model": config.model if config is not None and any(
                case.mode != "optimize" for case in cases) else None,
            "judge_model": judge_config.model if judge_config is not None and args.judge == "llm" else None,
            "judge_policy": "qualitative only; cannot override deterministic checks or circuit verification",
            "source_sha256": {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                              for path in sources},
            "cache_policy": "fresh database per case, strategy and seed; no cross-arm reuse",
            "lessons": "retrieval disabled for this benchmark; proposals remain unvalidated. "
                       "Run eval.learning for sequential held-out-guarded ablation.",
            "seed_scope": "random search only; production agent uses its unchanged deterministic start",
            "conditions": [corner.model_dump() for corner in CORNERS],
        }
        (out_dir / "manifest.json").write_text(json_text(manifest) + "\n", encoding="utf-8")
        results = []
        failures = False
        for case in cases:
            for strategy in strategies:
                # The agent is measured once, not repeated with an ineffectual seed.
                for seed in (seeds if strategy == "random" else [0]):
                    print(f"Evaluating {case.id}: {strategy}, seed={seed}", flush=True)
                    result = run_case(case, strategy, seed, args.budget, out_dir)
                    results.append(result)
                    failures |= result["status"] in ("error", "config_error")
                    (out_dir / "results.json").write_text(json_text(results) + "\n", encoding="utf-8")
                    from eval.checks import heuristic_checks
                    result["heuristic_checks"] = heuristic_checks(result)
                    statuses = {check["status"] for check in result["heuristic_checks"]}
                    result["heuristic_status"] = (
                        "fail" if "fail" in statuses else "missing" if "missing" in statuses else "pass"
                    )
                    failures |= result["heuristic_status"] == "fail"
                    case_dir = out_dir / f"{case.id}_{strategy}_s{seed}"
                    (case_dir / "result.json").write_text(json_text(result) + "\n", encoding="utf-8")
                    (out_dir / "results.json").write_text(json_text(results) + "\n", encoding="utf-8")
                    if args.judge == "llm":
                        from eval.judge import judge_result
                        if judge_config is None:
                            raise ValueError("LLM judge configuration is missing.")
                        print(f"Judging {case.id}: {strategy}", flush=True)
                        result["llm_judge"] = judge_result(
                            result, lambda messages: chat_completion(judge_config, messages),
                            judge_config.model, case_dir,
                        )
                        failures |= result["llm_judge"]["status"] == "error"
                    else:
                        result["llm_judge"] = {
                            "status": "not_run",
                            "reason": "disabled",
                        }
                    (case_dir / "result.json").write_text(json_text(result) + "\n", encoding="utf-8")
                    (out_dir / "results.json").write_text(json_text(results) + "\n", encoding="utf-8")
        from eval.report import write_report
        write_report(out_dir)
        print(f"Report: {out_dir / 'report.md'}")
        return 1 if failures else 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.TimeoutExpired) as error:
        print(f"EVAL ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
