"""Paired sequential lesson ablation with cold caches and fixed held-out promotion."""

import argparse
from datetime import datetime, timezone
import hashlib
from html import escape
import json
from pathlib import Path
from typing import Literal
from uuid import uuid4

from agent.lessons import validate_lesson
from agent.memory import Memory
from agent.planner import LLMConfig
from agent.sim import Candidate
from agent.smoke import ROOT
from agent.tracer import json_text
from eval.checks import heuristic_checks
from eval.run_eval import EvalCase, EvalSuite, SUITE, run_case


def write_learning_report(
    directory: Path, sequence: list[dict], strategy: str = "random",
) -> None:
    planner = "the actual model planner" if strategy == "agent" else "the seeded random planner"
    lines = [
        "# Sequential lesson ablation", "",
        f"Both arms use {planner} and the production scheduler, budgets, DC/AC "
        "gates and verifier. Every task has a cold independent cache. Only the with-lessons arm "
        "receives previously held-out-validated candidate priors; each prior is reverified.",
        "This measures reusable-candidate learning, not model fine-tuning or statistically "
        "significant generalization. Held-out validation compute is reported separately.",
        "Remote model calls are not seed-deterministic; these are observed paired results, "
        "not a causal estimate isolating reuse from model variability." if strategy == "agent" else
        "Both random arms use identical seeds.",
        "", "| Task | Without: first pass / total | With: first pass / total | Prior | Promotion | Model calls without / with |",
        "|---|---|---|---|---|---|",
    ]
    svg = ['<svg xmlns="http://www.w3.org/2000/svg" width="840" height="420" viewBox="0 0 840 420">',
           '<rect width="840" height="420" fill="white"/>',
           '<text x="20" y="24">Executions to first fully verified design (cross = failure at budget)</text>']
    maximum = max((row[arm]["budget"] for row in sequence for arm in ("without", "with")), default=40)
    for arm, color, offset in (("without", "#c66", -3), ("with", "#2675bd", 3)):
        points = []
        for index, row in enumerate(sequence):
            score = row[arm]
            x = 70 + index * 680 / max(1, len(sequence) - 1) + offset
            cost = score["first_pass"] if score["verified_success"] else score["budget"]
            y = 340 - 280 * cost / maximum
            if score["verified_success"]:
                points.append(f"{x:.2f},{y:.2f}")
                svg.append(f'<circle cx="{x}" cy="{y}" r="5" fill="{color}"/>')
            else:
                # Do not connect failed tasks into a success-cost curve.
                if points:
                    svg.append(f'<polyline points="{" ".join(points)}" fill="none" stroke="{color}"/>')
                    points = []
                svg.append(f'<path d="M{x-4},{y-4} l8,8 m-8,0 l8,-8" stroke="{color}"/>')
            if arm == "with":
                svg.append(f'<text x="{x-30}" y="370" font-size="12">{escape(row["case_id"])}</text>')
        if points:
            svg.append(f'<polyline points="{" ".join(points)}" fill="none" stroke="{color}"/>')
        svg.append(f'<text x="{30 if arm == "without" else 360}" y="404" fill="{color}">{arm} lessons</text>')
    svg += ['<text x="10" y="60">budget</text>', '<text x="25" y="345">0</text>', '</svg>']
    for row in sequence:
        def display(arm: str) -> str:
            score = row[arm]
            first = score["first_pass"] if score["verified_success"] else "failed"
            return f"{first} / {score['simulations_used']}"
        lines.append(f"| {row['case_id']} | {display('without')} | {display('with')} | "
                     f"{row['prior_used']} | {row['promotion']} | "
                     f"{row['without'].get('model_calls', 0)} / {row['with'].get('model_calls', 0)} |")
    lines += ["", f"Validation executions: {sum(row.get('validation_simulations', 0) for row in sequence)}.",
              f"Search executions without lessons: {sum(row['without']['simulations_used'] for row in sequence)}; "
              f"with lessons: {sum(row['with']['simulations_used'] for row in sequence)}.",
              "", "Validation is additional compute, not a free improvement. Compare combined search "
              "and validation cost before claiming a net saving. Failures are not assigned a "
              "zero-cost success. See sequence.json and per-task traces."]
    lines += ["", f"Search model calls: {sum(row[arm].get('model_calls', 0) for row in sequence for arm in ('without', 'with'))}; "
              f"validation model calls: {sum(row.get('validation_model_calls', 0) for row in sequence)}.",
              f"Search reported tokens: {sum(row[arm].get('token_usage', {}).get('total_tokens', 0) for row in sequence for arm in ('without', 'with'))}. "
              "Per-arm wall time and token usage are in sequence.json; validation usage is in validation.json."]
    if not any(row["prior_used"] for row in sequence):
        lines += ["", "No validated prior was reused; this sequence does not demonstrate learning."]
    (directory / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (directory / "learning.svg").write_text("\n".join(svg), encoding="utf-8")


def run_sequence(
    directory: Path, cases: list[EvalCase], *, seed: int = 0, budget: int = 40,
    strategy: Literal["random", "agent"] = "random",
) -> list[dict]:
    if strategy not in ("random", "agent") or not cases or budget < 1:
        raise ValueError("Choose a planner strategy, optimization cases and a positive budget.")
    if any(case.mode != "optimize" for case in cases):
        raise ValueError("Learning comparisons require optimization cases.")
    directory.mkdir(parents=True, exist_ok=False)
    sources = [*sorted((ROOT / "agent").glob("*.py")), *sorted((ROOT / "eval").glob("*.py")),
               ROOT / "eval" / "holdout.json", SUITE, ROOT / "models" / "ptm180.lib",
               ROOT / "circuits" / "ota5t.cir.j2"]
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(), "seed": seed, "budget": budget,
        "strategy": strategy,
        "planner_model": LLMConfig.from_env().model if strategy == "agent" else "seeded-random",
        "cases": [case.model_dump(mode="json") for case in cases],
        "source_sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in sources},
        "scope": "Cold-cache sequential candidate-prior ablation, not LLM self-training.",
    }
    (directory / "manifest.json").write_text(json_text(manifest) + "\n", encoding="utf-8")
    sequence = []
    prior = None
    for index, case in enumerate(cases):
        print(f"Learning {strategy}: {case.id}, without / with validated priors", flush=True)
        baseline = run_case(case, strategy, seed, budget, directory / f"{index}_without")
        proposed = run_case(case, strategy, seed, budget, directory / f"{index}_with",
                            warm_candidate=prior)
        for result in (baseline, proposed):
            if not all(check["status"] == "pass" for check in heuristic_checks(result)):
                raise ValueError(f"{case.id}: lesson ablation integrity checks failed.")

        def score(result: dict) -> dict:
            return {key: result[key] for key in ("verified_success", "simulations_used", "budget",
                                                "simulations_to_first_pass", "trace", "model_calls",
                                                "token_usage", "wall_clock_s")} | {
                "first_pass": result["simulations_to_first_pass"]}

        row = {"case_id": case.id, "without": score(baseline), "with": score(proposed),
               "prior_used": prior is not None,
               "prior_candidate": prior.model_dump() if prior else None,
               "promotion": "no_verified_candidate"}
        sequence.append(row)
        (directory / "sequence.json").write_text(json_text(sequence) + "\n", encoding="utf-8")
        write_learning_report(directory, sequence, strategy)
        memory = Memory(directory / f"{index}_with" / f"{case.id}_{strategy}_s{seed}" / "memory.sqlite3")
        records = [record for record in memory.lesson_records(validated_only=False)
                   if record["candidate_json"] is not None]
        if records:
            record = records[-1]
            row["promotion"] = "pending_validation"
            (directory / "sequence.json").write_text(json_text(sequence) + "\n", encoding="utf-8")
            write_learning_report(directory, sequence, strategy)
            print(f"Validating {case.id} lesson on the fixed held-out suite ({strategy})", flush=True)
            evidence = validate_lesson(memory, record["id"], directory / f"{index}_validation",
                                       budget=budget, strategy=strategy)
            row["validation_simulations"] = sum(
                item[arm]["simulations_used"] for item in evidence["pairs"]
                for arm in ("baseline", "proposed"))
            row["validation_model_calls"] = sum(
                item[arm].get("model_calls", 0) for item in evidence["pairs"]
                for arm in ("baseline", "proposed"))
            row["validation"] = str(directory / f"{index}_validation" / "validation.json")
            row["promotion"] = "accepted" if evidence["accepted"] else "rejected"
            if evidence["accepted"]:
                prior = Candidate.model_validate_json(record["candidate_json"])
        (directory / "sequence.json").write_text(json_text(sequence) + "\n", encoding="utf-8")
        write_learning_report(directory, sequence, strategy)
    return sequence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", action="append")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--budget", type=int, default=40)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--strategy", choices=("random", "agent"), default="random")
    args = parser.parse_args(argv)
    try:
        suite = EvalSuite.model_validate_json(SUITE.read_text())
        ids = args.task or ["easy", "gain33", "gain36", "gain40"]
        by_id = {case.id: case for case in suite.cases if case.mode == "optimize"}
        if args.budget < 1 or any(name not in by_id for name in ids):
            raise ValueError("Choose known optimization cases and a positive budget.")
        directory = args.out or ROOT / "runs" / "learning" / uuid4().hex
        run_sequence(directory, [by_id[name] for name in ids], seed=args.seed,
                     budget=args.budget, strategy=args.strategy)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"LEARNING EVAL FAIL: {error}")
        return 2
    print(f"Learning report: {directory / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
