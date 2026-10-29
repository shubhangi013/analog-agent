"""Simulator-derived reusable candidates, guarded by a fixed held-out comparison."""

import argparse
import hashlib
import json
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from agent.smoke import ROOT


HOLDOUT = ROOT / "eval" / "holdout.json"


class ValidationScore(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    integrity: bool
    verified_success: bool
    budget: int = Field(gt=0)
    first_pass: int | None = Field(default=None, gt=0)
    simulations_used: int = Field(ge=0)
    trace: str
    model_calls: int = Field(default=0, ge=0)
    wall_clock_s: float = Field(default=0, ge=0)
    token_usage: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def consistent_cost(self) -> "ValidationScore":
        if self.simulations_used > self.budget or (
            self.verified_success != (self.first_pass is not None)
        ) or (self.first_pass is not None and self.first_pass > self.simulations_used):
            raise ValueError("Inconsistent lesson-validation success or execution counts.")
        return self


class ValidationPair(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    case_id: str
    baseline: ValidationScore
    proposed: ValidationScore


class ValidationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    heldout_sha: str
    candidate_sha: str = Field(pattern=r"^[a-f0-9]{64}$")
    provenance: str = Field(min_length=1)
    pairs: list[ValidationPair]
    metric: str = ""
    accepted: bool | None = None
    strategy: Literal["random", "agent"] = "random"
    planner_model: str = "seeded-random"


def accepts_validation(evidence: dict) -> bool:
    try:
        validation = ValidationEvidence.model_validate(evidence)
    except ValidationError:
        return False
    if validation.heldout_sha != hashlib.sha256(HOLDOUT.read_bytes()).hexdigest():
        return False
    expected = {case["id"] for case in json.loads(HOLDOUT.read_text())["cases"]}
    if len(validation.pairs) != len(expected) or {pair.case_id for pair in validation.pairs} != expected:
        return False
    for pair in validation.pairs:
        baseline, proposed = pair.baseline, pair.proposed
        if not baseline.integrity or not proposed.integrity:
            return False
        if baseline.budget != proposed.budget:
            return False
        if baseline.verified_success:
            if not proposed.verified_success:
                return False
            if proposed.first_pass is None or baseline.first_pass is None:
                return False
            if proposed.first_pass > baseline.first_pass:
                return False
        elif not proposed.verified_success and proposed.simulations_used > baseline.simulations_used:
            return False
    return True


def validate_lesson(
    memory, lesson_id: int, directory: Path, *, budget: int = 40,
    strategy: Literal["random", "agent"] = "random",
) -> dict:
    from agent.memory import CachedSimulator
    from agent.sim import Candidate
    from agent.tracer import Tracer, json_text
    from eval.checks import heuristic_checks
    from eval.run_eval import EvalSuite, run_case
    from agent.planner import LLMConfig

    record = next((row for row in memory.lesson_records(validated_only=False)
                   if row["id"] == lesson_id), None)
    if record is None or record["candidate_json"] is None:
        raise ValueError("A lesson needs a simulator-verified candidate before validation.")
    if record["circuit_sha"]:
        raise ValueError("The fixed held-out validator currently supports built-in OTA lessons only.")
    directory.mkdir(parents=True, exist_ok=False)
    candidate = Candidate.model_validate_json(record["candidate_json"])
    provenance = CachedSimulator(memory, Tracer("validation", "provenance", directory)).provenance()
    if record["provenance"] != provenance:
        raise ValueError("Lesson simulator/model provenance changed; rerun its source task.")
    suite = EvalSuite.model_validate_json(HOLDOUT.read_text())
    pairs = []
    for case in suite.cases:
        baseline_root, proposed_root = directory / "baseline", directory / "proposed"
        baseline = run_case(case, strategy, 0, budget, baseline_root)
        proposed = run_case(case, strategy, 0, budget, proposed_root, warm_candidate=candidate)

        def score(result: dict) -> dict:
            return {
                "integrity": all(check["status"] == "pass" for check in heuristic_checks(result)),
                "verified_success": result["verified_success"], "budget": result["budget"],
                "first_pass": result["simulations_to_first_pass"],
                "simulations_used": result["simulations_used"], "trace": result["trace"],
                "model_calls": result["model_calls"], "wall_clock_s": result["wall_clock_s"],
                "token_usage": result.get("token_usage", {}),
            }

        pairs.append({"case_id": case.id, "baseline": score(baseline), "proposed": score(proposed)})
    evidence = {
        "heldout_sha": hashlib.sha256(HOLDOUT.read_bytes()).hexdigest(),
        "candidate_sha": hashlib.sha256(record["candidate_json"].encode()).hexdigest(),
        "provenance": provenance, "pairs": pairs,
        "strategy": strategy,
        "planner_model": LLMConfig.from_env().model if strategy == "agent" else "seeded-random",
        "metric": "Preserve success and do not increase executions to first verified design; "
                  "on mutual failures do not increase total executions.",
    }
    if CachedSimulator(memory, Tracer("validation", "final", directory)).provenance() != provenance:
        raise ValueError("Simulation provenance changed during lesson validation.")
    evidence["accepted"] = memory.promote_lesson(lesson_id, evidence)
    (directory / "validation.json").write_text(json_text(evidence) + "\n", encoding="utf-8")
    return evidence


def main(argv: list[str] | None = None) -> int:
    from agent.memory import Memory
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lesson", required=True, type=int)
    parser.add_argument("--memory", type=Path, default=ROOT / "runs" / "memory.sqlite3")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        evidence = validate_lesson(Memory(args.memory), args.lesson,
                                   args.out or ROOT / "runs" / "lesson_validation" / uuid4().hex)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"LESSON VALIDATION FAIL: {error}")
        return 2
    print(f"Lesson {args.lesson}: {'accepted' if evidence['accepted'] else 'rejected'}")
    return 0 if evidence["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
