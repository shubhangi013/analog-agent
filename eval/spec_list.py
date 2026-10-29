"""Evaluate a YAML engineer-spec list through actual intake, search and both assessment layers."""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import subprocess
import time
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
import yaml

from agent.intake import ClarificationNeeded, interpret_request, route_request
from agent.orchestrator import CORNERS, WORKERS, load_targets
from agent.planner import LLMConfig, chat_completion
from agent.reasoning import Reasoner
from agent.sim import ngspice_version
from agent.smoke import ROOT, find_ngspice
from agent.spec import Objective, Request
from agent.tracer import TraceEvent, Tracer, json_text
from agent.verifier import Targets
from eval.checks import heuristic_checks
from eval.judge import judge_result
from eval.report import write_report
from eval.run_eval import EvalCase, run_case


FIELDS = {"gain_db_min": "gain_db", "ugf_hz_min": "ugf_hz",
          "pm_deg_min": "pm_deg", "power_w_max": "power_w"}


class SpecTargets(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    gain_db_min: float | None = Field(ge=0, default=None)
    ugf_hz_min: float | None = Field(gt=0, default=None)
    pm_deg_min: float | None = Field(gt=0, default=None)
    power_w_max: float | None = Field(gt=0, default=None)


class EngineerSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,60}$")
    category: Literal["easy", "medium", "hard", "objective", "infeasible", "parser"]
    objective: Objective
    targets: SpecTargets
    spec_text: str = Field(min_length=1, max_length=2000)
    expected: Literal["feasible", "borderline", "likely_infeasible", "infeasible", "clarify"]
    why: str


class SpecList(BaseModel):
    model_config = ConfigDict(extra="forbid")
    specs: list[EngineerSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_ids(self) -> "SpecList":
        if len({spec.id for spec in self.specs}) != len(self.specs):
            raise ValueError("Specification ids must be unique.")
        return self


def intake_contract(spec: EngineerSpec, request: Request) -> list[dict]:
    checks = [
        {"name": "intake_objective", "status": "pass" if request.objective == spec.objective else "fail",
         "evidence": f"Source objective={spec.objective}; interpreted={request.objective}."},
    ]
    for external, internal in FIELDS.items():
        expected = getattr(spec.targets, external)
        actual = getattr(request.targets, internal)
        if expected is not None:
            checks.append({
                "name": f"intake_{internal}",
                "status": "pass" if math.isclose(actual, expected, rel_tol=1e-12, abs_tol=0) else "fail",
                "evidence": f"Independent source target={expected:g}; parsed={actual:g} in SI units.",
            })
        else:
            disclosed = any(f"No {internal} target supplied" in text for text in request.assumptions)
            checks.append({
                "name": f"default_{internal}", "status": "pass" if disclosed else "fail",
                "evidence": f"Source target is null; production default {actual:g} must be disclosed.",
            })
    return checks


def resolve_spec(spec: EngineerSpec, defaults: Targets, directory: Path, *, resume: bool = False) -> dict:
    record_path = directory / "intake.json"
    if resume and record_path.exists():
        record = json.loads(record_path.read_text(encoding="utf-8"))
        if record["status"] == "accepted":
            record["checks"] = intake_contract(spec, Request.model_validate(record["request"]))
            if any(check["status"] == "fail" for check in record["checks"]):
                raise ValueError(f"Saved intake for {spec.id} no longer matches the source contract.")
        return record
    directory.mkdir(parents=True, exist_ok=resume)
    tracer = Tracer(spec.id, "intake", directory)
    started = time.perf_counter()
    tracer.emit(TraceEvent(type="task_start", inputs={"goal": spec.spec_text, "mode": "intake"}))
    reasoner, request, checks = None, None, []
    status, error = "accepted", None
    try:
        request, pending = tracer.tool("route_request", route_request, spec.spec_text, defaults)
        if pending is not None:
            reasoner = Reasoner(tracer)
            request = interpret_request(pending, defaults, reasoner)
        if request is None:
            raise ValueError("Intake returned neither a request nor a clarification.")
        checks = intake_contract(spec, request)
        if any(check["status"] == "fail" for check in checks):
            status, error = "intake_mismatch", "Parsed request disagrees with the independent spec contract."
    except ClarificationNeeded as failure:
        status, error = "clarification_required", str(failure)
        checks = [{"name": "clarification_without_simulation", "status": "pass", "evidence": error}]
    except (OSError, ValueError, RuntimeError) as failure:
        status, error = "intake_error", str(failure)
        checks = [{"name": "intake_execution", "status": "fail", "evidence": error}]
    elapsed = time.perf_counter() - started
    tracer.emit(TraceEvent(type="task_end", duration_ms=elapsed * 1000, outputs={
        "outcome": status, "simulations_used": 0, "cache_hits": 0, "wall_clock_s": elapsed,
        "error": error, "engineer_verdict": "automatic_eval_approval_not_human_review",
    }))
    record = {
        "status": status, "error": error, "request": request.model_dump(mode="json") if request else None,
        "trace": str(tracer.path), "model_calls": reasoner.calls if reasoner else 0,
        "wall_clock_s": elapsed, "checks": checks,
    }
    (directory / "intake.json").write_text(json_text(record) + "\n", encoding="utf-8")
    return record


def write_spec_report(directory: Path, specs: list[dict], results: list[dict]) -> None:
    strategies = sorted({result["strategy"] for result in results if result["mode"] == "optimize"})
    lines = [
        "# Engineer-spec list results", "",
        f"Processed specifications: {len(specs)}. All source expectations are estimates, not ground truth.",
        "A search failure within budget is not proof of infeasibility. No failed task is scored as a zero-cost success.",
        "Null targets use the agent's disclosed production defaults, not unconstrained optimization. "
        "Results for those rows are conditional on those additional default constraints.",
        "Source text is parsed before search and checked against independent YAML quantities/objective. "
        "Mismatched or unresolved requests are not simulated. Intake is shared between search arms; "
        "each search has a cold independent cache. Export is excluded and approval is automatic.",
        "", "| Category | Specs | Accepted intake | Clarification | Intake errors/mismatches |",
        "|---|---:|---:|---:|---:|",
    ]
    for category in ("easy", "medium", "hard", "objective", "infeasible", "parser"):
        rows = [spec for spec in specs if spec["category"] == category]
        statuses = Counter(row["intake"]["status"] for row in rows)
        lines.append(f"| {category} | {len(rows)} | {statuses['accepted']} | "
                     f"{statuses['clarification_required']} | "
                     f"{statuses['intake_error'] + statuses['intake_mismatch']} |")
    lines += ["", "| Strategy | Verified designs / attempted | Simulations | Hard-check failures | Judge mean /4 |",
              "|---|---:|---:|---:|---:|"]
    for strategy in strategies:
        rows = [result for result in results if result["strategy"] == strategy]
        scores = [row["llm_judge"]["mean_score"] for row in rows
                  if row.get("llm_judge", {}).get("status") == "scored"]
        mean = f"{sum(scores)/len(scores):.3f}" if scores else "not scored"
        lines.append(f"| {strategy} | {sum(row['verified_success'] for row in rows)} / {len(rows)} | "
                     f"{sum(row['simulations_used'] for row in rows)} | "
                     f"{sum(row['heuristic_status'] == 'fail' for row in rows)} | {mean} |")
    lines += ["", "| Spec | Expectation (not oracle) | Intake | Search outcomes |",
              "|---|---|---|---|"]
    for spec in specs:
        rows = [row for row in results if row["case_id"] == spec["id"] and row["mode"] == "optimize"]
        outcomes = "; ".join(f"{row['strategy']}: {row['status']}, {row['simulations_used']} simulations"
                             for row in rows) or spec["intake"].get("error") or "-"
        lines.append(f"| {spec['id']} | {spec['expected']} | {spec['intake']['status']} | "
                     f"{outcomes.replace('|', '/').replace(chr(10), ' ')} |")
    lines += ["", "Detailed qualitative assessments and measured alternatives: report.md. "
              "Per-spec intake, assumptions and independent contract checks: spec_results.json. "
              "Per-task measurements, model/judge usage, failures and traces: results.json and case folders.", ""]
    (directory / "spec_report.md").write_text("\n".join(lines), encoding="utf-8")


def evaluate_spec(
    spec: EngineerSpec, directory: Path, defaults: Targets, config: LLMConfig, *,
    strategies: tuple[str, ...], budget: int, seed: int, judge: bool, resume: bool,
) -> tuple[dict, list[dict]]:
    print(f"Spec {spec.id} ({spec.category}): intake", flush=True)
    intake = resolve_spec(spec, defaults, directory / f"{spec.id}_intake", resume=resume)
    results = []
    request = Request.model_validate(intake["request"]) if intake["status"] == "accepted" else None
    arms = strategies if request is not None else ("intake",)
    for arm in arms:
        case_directory = (directory / f"{spec.id}_{arm}_s{seed}" if request is not None
                          else directory / f"{spec.id}_intake")
        result_path = case_directory / "result.json"
        saved = json.loads(result_path.read_text(encoding="utf-8")) if resume and result_path.exists() else None
        if saved is not None and "llm_judge" in saved:
            print(f"Spec {spec.id}: reusing completed {arm} evidence", flush=True)
            results.append(saved)
            continue
        if request is not None:
            case = EvalCase(
                id=spec.id, mode="optimize", goal=spec.spec_text, targets=request.targets,
                objective=request.objective, expected_feasible=True,
                difficulty=spec.category if spec.category in ("easy", "medium", "hard") else "hard",
            )
            print(f"Spec {spec.id}: {arm}, budget={budget}", flush=True)
            result = run_case(case, arm, seed, budget, directory, resolved_request=request, resume=resume)
            result["expected_feasible"] = None
            result["heuristic_checks"] = intake["checks"] + heuristic_checks(result)
        else:
            result = {
                "case_id": spec.id, "mode": "intake", "strategy": "intake", "seed": seed,
                "goal": spec.spec_text, "targets": spec.targets.model_dump(),
                "requested_objective": spec.objective, "status": intake["status"],
                "verified_success": False, "simulations_used": 0, "budget": budget, "cache_hits": 0,
                "wall_clock_s": intake["wall_clock_s"], "model_calls": intake["model_calls"],
                "trace": intake["trace"], "review_text": intake["error"],
                "heuristic_checks": intake["checks"], "engineer_verdict": "not_requested_intake",
                "candidates": [], "pareto_options": [], "progress": [],
            }
        result.update(intake=intake, reference_expectation=spec.expected, category=spec.category)
        result["heuristic_status"] = ("fail" if any(
            check["status"] == "fail" for check in result["heuristic_checks"]) else "pass")
        result["llm_judge"] = (
            judge_result(result, lambda messages: chat_completion(config, messages),
                         config.model, case_directory)
            if judge else {"status": "not_run", "reason": "disabled"})
        result_path.write_text(json_text(result) + "\n", encoding="utf-8")
        results.append(result)
    return spec.model_dump(mode="json") | {"intake": intake}, results


def run_specs(
    source: Path, directory: Path, *, strategy: Literal["agent", "random", "both"] = "both",
    budget: int = 40, seed: int = 0, tasks: list[str] | None = None, judge: bool = True,
    case_workers: int = 1, resume: bool = False,
) -> list[dict]:
    source_bytes = source.read_bytes()
    suite = SpecList.model_validate(yaml.safe_load(source_bytes))
    selected = set(tasks or [spec.id for spec in suite.specs])
    if budget < 1 or selected - {spec.id for spec in suite.specs} or not 1 <= case_workers <= 8:
        raise ValueError("Choose known spec ids, a positive budget and 1-8 concurrent specifications.")
    config = LLMConfig.from_env()
    version = ngspice_version(find_ngspice())
    defaults = load_targets("easy")
    sources = [*sorted((ROOT / "agent").glob("*.py")), *sorted((ROOT / "eval").glob("*.py")),
               *sorted((ROOT / "prompts").glob("*.md")),
               ROOT / "models" / "ptm180.lib", ROOT / "circuits" / "ota5t.cir.j2"]
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "selected_specs": sorted(selected), "strategy": strategy, "seed": seed,
        "budget": budget, "workers": WORKERS, "case_workers": case_workers,
        "model": config.model, "judge": judge,
        "defaults": defaults.model_dump(), "ngspice_version": version,
        "conditions": [condition.model_dump() for condition in CORNERS],
        "code_sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in sources},
        "scope": "Source expectations are hypotheses; null targets retain disclosed production defaults.",
    }
    manifest_path = directory / "manifest.json"
    if resume:
        original = json.loads(manifest_path.read_text(encoding="utf-8"))
        for key in ("source_sha256", "selected_specs", "strategy", "seed", "budget",
                    "model", "judge", "defaults", "ngspice_version", "conditions", "workers"):
            if original[key] != manifest[key]:
                raise ValueError(f"Cannot resume with changed {key}.")
        for path, digest in original["code_sha256"].items():
            if path not in ("eval\\spec_list.py", "eval\\run_eval.py") and manifest["code_sha256"].get(path) != digest:
                raise ValueError(f"Cannot resume after changing {path}. Start a fresh evaluation.")
        original.setdefault("resumes", []).append(manifest)
        manifest_path.write_text(json_text(original) + "\n", encoding="utf-8")
    else:
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "source_specs.yaml").write_bytes(source_bytes)
        manifest_path.write_text(json_text(manifest) + "\n", encoding="utf-8")
    results, records = [], []
    strategies = ("agent", "random") if strategy == "both" else (strategy,)
    specs = [spec for spec in suite.specs if spec.id in selected]
    order = {spec.id: index for index, spec in enumerate(specs)}
    with ThreadPoolExecutor(max_workers=case_workers) as pool:
        futures = [pool.submit(
            evaluate_spec, spec, directory, defaults, config, strategies=strategies,
            budget=budget, seed=seed, judge=judge, resume=resume,
        ) for spec in specs]
        for future in as_completed(futures):
            record, current = future.result()
            records.append(record)
            results.extend(current)
            records.sort(key=lambda row: order[row["id"]])
            results.sort(key=lambda row: (order[row["case_id"]], row["strategy"]))
            (directory / "results.json").write_text(json_text(results) + "\n", encoding="utf-8")
            (directory / "spec_results.json").write_text(json_text(records) + "\n", encoding="utf-8")
            write_spec_report(directory, records, results)
            write_report(directory)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--specs", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--strategy", choices=("agent", "random", "both"), default="both")
    parser.add_argument("--budget", type=int, default=40)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--task", action="append")
    parser.add_argument("--judge", choices=("llm", "off"), default="llm")
    parser.add_argument("--case-workers", type=int, default=4,
                        help="Concurrent specifications; each search still has its own four-worker limit.")
    parser.add_argument("--resume", action="store_true", help="Reuse completed evidence and resume search checkpoints.")
    args = parser.parse_args(argv)
    try:
        directory = args.out or ROOT / "runs" / "eval" / f"spec_list_{uuid4().hex}"
        if args.resume and args.out is None:
            raise ValueError("--resume requires the original --out directory.")
        results = run_specs(args.specs, directory, strategy=args.strategy, budget=args.budget,
                            seed=args.seed, tasks=args.task, judge=args.judge == "llm",
                            case_workers=args.case_workers, resume=args.resume)
        print(f"Spec report: {directory / 'spec_report.md'}")
        return int(any(result["heuristic_status"] == "fail"
                       or result["status"] in ("error", "intake_error", "intake_mismatch")
                       or result["llm_judge"]["status"] == "error" for result in results))
    except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.TimeoutExpired, yaml.YAMLError) as error:
        print(f"SPEC EVAL ERROR: {error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
