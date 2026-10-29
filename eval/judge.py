"""Evidence-bounded LLM assessment of reasoning, never electrical pass/fail."""

import json
from pathlib import Path
import time

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.planner import ChatFunction, LLMError
from agent.smoke import ROOT
from agent.tracer import json_text


DIMENSIONS = (
    "goal_alignment", "numerical_grounding", "tradeoff_reasoning",
    "experimental_reasoning", "limitation_honesty",
)


class Judgment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    score: int = Field(ge=0, le=4)
    rationale: str = Field(min_length=1, max_length=1200)
    evidence: list[str] = Field(min_length=1, max_length=6)


class JudgeReply(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    dimensions: dict[str, Judgment]
    summary: str = Field(min_length=1, max_length=1500)

    @model_validator(mode="after")
    def complete_rubric(self) -> "JudgeReply":
        if set(self.dimensions) != set(DIMENSIONS):
            raise ValueError(f"Expected exactly these rubric dimensions: {', '.join(DIMENSIONS)}")
        return self


def build_evidence(result: dict) -> dict:
    """Only bounded structured results, hypotheses and actual review text; never SPICE logs."""
    items = {
        "task": {
            "goal": result.get("goal"), "targets": result.get("targets"),
            "mode": result["mode"], "expected_feasible": result.get("expected_feasible"),
            "requested_objective": result.get("requested_objective"),
            "reference_expectation": result.get("reference_expectation"),
            "reference_expectation_is_only_a_hypothesis": True,
        },
        "outcome": {
            key: result.get(key) for key in (
                "status", "verified_success", "simulations_used", "budget",
                "model_calls", "fallback_calls", "simulation_errors",
            )
        },
        "scope": {
            "goals_parsed_as_natural_language": bool(result.get("input_spec")),
            "objective_minimization_implemented": True,
            "optimization_scope": "Best observed, not a proof of global optimality.",
            "interactive_options_explanation_implemented": True,
            "lessons_learning_implemented": True,
            "lessons_retrieval_enabled_in_this_benchmark": False,
            "strategy": result["strategy"],
            "engineer_verdict": result.get("engineer_verdict"),
            "archive_is_evaluation_generated_not_product_output": True,
        },
    }
    if "intake" in result:
        items["intake"] = result["intake"]
    for index, artifact in enumerate(result.get("reasoning_artifacts", [])[-4:]):
        items[f"reasoning_{index}"] = artifact
    options = result.get("pareto_options", [])
    for index, option in enumerate(options[:3]):
        items[f"option_{index}"] = {
            key: option[key] for key in ("candidate_id", "candidate", "worst_case_metrics", "passed")
        }
    items["review"] = result.get("review_text", "(No final engineering review was presented.)")
    items["heuristics"] = result.get("heuristic_checks", [])
    observed = [
        {
            "candidate_id": row["candidate_id"], "condition": name,
            "metrics": data["metrics"], "passed": data["passed"],
        }
        for row in result.get("candidates", []) for name, data in row["conditions"].items()
    ]
    items["observed_results"] = observed if len(observed) <= 12 else observed[:6] + observed[-6:]
    if result.get("probes"):
        items["diagnostic_probes"] = [
            {"name": probe["name"], "bias": probe["result"]["bias"],
             "nodes": probe["result"].get("nodes", {}), "fingerprint": probe["fingerprint"],
             "dc_power_w": probe["result"].get("dc_power_w"),
             "bias_voltage_v": probe["result"].get("bias_voltage_v"),
             "tail_current_a": probe["result"].get("tail_current_a"),
             "requested_bias_ua": probe["candidate"]["I_bias"],
             "fixed_bias_v": probe.get("fixed_bias_v"),
             "analysis": probe["result"]["analysis"],
             "transient": probe["result"].get("transient")}
            for probe in result["probes"][:2] + result["probes"][2:][-4:]
        ]
        items["diagnostic_hypotheses"] = result.get("hypotheses", [])[:3]
        from agent.diagnostics import components
        from agent.sim import Candidate, Condition
        baseline = result["probes"][0]
        items["circuit_components"] = components(
            baseline["circuit_body"], Candidate.model_validate(baseline["candidate"]),
            Condition.model_validate(baseline["result"]["condition"]))
        items["circuit_connections"] = result.get("connections", {})
        items["diagnostic_scope"] = (
            "Both strategy labels use the same bounded model diagnostic loop, not random diagnosis. "
            "The baseline DC sweep calibrates gate bias to requested_bias_ua; subsequent probes "
            "with fixed_bias_v bypass recalibration and hold that measured voltage. An explicit "
            "I_bias intervention deliberately recalibrates. Transient uses its associated DC bias. "
            "Transient check: after settling, rail occupancy above five percent or normalized "
            "sine-fit residual above a tenth is clipping/distortion in this fixed-stimulus scenario."
        )
    return items


def judge_result(result: dict, chat: ChatFunction, model_name: str, directory: Path) -> dict:
    evidence = build_evidence(result)
    system = (ROOT / "prompts" / "eval_judge.md").read_text(encoding="utf-8")
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": json_text(evidence)},
    ]
    attempts = []
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "judge_input.json").write_text(json_text({
        "model": model_name, "messages": messages,
    }) + "\n", encoding="utf-8")
    for attempt in (1, 2):
        started = time.perf_counter()
        response = None
        try:
            response = chat(messages)
            reply = JudgeReply.model_validate_json(response.content)
            unknown = {
                ref for judgment in reply.dimensions.values() for ref in judgment.evidence
                if ref not in evidence
            }
            if unknown:
                raise ValueError(f"Unknown evidence ids: {', '.join(sorted(unknown))}")
            attempts.append({
                "attempt": attempt, "duration_ms": (time.perf_counter() - started) * 1000,
                "usage": response.usage, "valid": True,
                "response": response.content,
            })
            judgment = {
                "status": "scored", "model": model_name, **reply.model_dump(),
                "mean_score": sum(item.score for item in reply.dimensions.values()) / len(DIMENSIONS),
                "attempts": attempts,
                "authority": "qualitative_only_cannot_override_heuristics_or_verifier",
            }
            break
        except (LLMError, ValueError) as error:
            attempts.append({
                "attempt": attempt, "duration_ms": (time.perf_counter() - started) * 1000,
                "valid": False, "error": str(error)[:1500],
                "usage": response.usage if response is not None else {},
                "response": response.content if response is not None else None,
            })
            if attempt == 1:
                messages.append({
                    "role": "user",
                    "content": "The assessment was rejected: " + str(error)[:800]
                    + ". Return the required JSON with only evidence ids present in the input.",
                })
    else:
        judgment = {
            "status": "error", "model": model_name, "attempts": attempts,
            "authority": "qualitative_only_cannot_override_heuristics_or_verifier",
        }
    (directory / "judge.json").write_text(json_text(judgment) + "\n", encoding="utf-8")
    return judgment
