"""Small typed tools; arithmetic and design checks never depend on the model."""

import ast
from collections.abc import Callable, Iterable, Mapping
import math
import operator
import random
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, ValidationError

from annotated_types import Ge, Le

from agent.sim import Candidate

if TYPE_CHECKING:
    from agent.memory import Memory, StoredDesign
    from agent.verifier import Targets


# Grid used when generating values: widths 0.1 um, lengths 0.01 um, current 0.1 uA.
STEPS = {"W": 0.1, "L": 0.01, "I": 0.1}


def candidate_ranges() -> dict[str, tuple[float, float]]:
    """Read the allowed ranges from Candidate itself so there is one source of truth."""
    ranges = {}
    for name, info in Candidate.model_fields.items():
        low = next(float(item.ge) for item in info.metadata if isinstance(item, Ge))
        high = next(float(item.le) for item in info.metadata if isinstance(item, Le))
        ranges[name] = (low, high)
    return ranges


def candidate_key(candidate: Candidate) -> tuple[float, ...]:
    return tuple(round(value, 6) for value in candidate.model_dump().values())


def snap(name: str, value: float) -> float:
    low, high = candidate_ranges()[name]
    step = STEPS[name[0]]
    value = round(round(value / step) * step, 6)
    return min(high, max(low, value))


def make_candidates(
    base: Candidate, n: int, spread: float, *,
    seed: int = 0, exclude: Iterable[Candidate] = (),
) -> list[Candidate]:
    """n distinct candidates, each value scaled by a random factor in [1/(1+spread), 1+spread]."""
    if n < 1 or not math.isfinite(spread) or spread <= 0:
        raise ValueError("make_candidates needs n >= 1 and a positive, finite spread.")
    rng = random.Random(seed)
    seen = {candidate_key(base), *(candidate_key(item) for item in exclude)}
    reach = math.log1p(spread)
    candidates: list[Candidate] = []
    for _ in range(n * 200):
        values = {
            name: snap(name, value * math.exp(rng.uniform(-reach, reach)))
            for name, value in base.model_dump().items()
        }
        candidate = Candidate(**values)
        if candidate_key(candidate) not in seen:
            seen.add(candidate_key(candidate))
            candidates.append(candidate)
            if len(candidates) == n:
                return candidates
    raise ValueError(f"Could not find {n} new candidates; increase spread or change the base.")


class Calculation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expression: str


def calc(expression: str) -> float:
    expression = Calculation(expression=expression).expression
    if len(expression) > 200:
        raise ValueError("Calculation expression exceeds 200 characters.")
    tree = ast.parse(expression, mode="eval")
    operations = {
        ast.Add: operator.add, ast.Sub: operator.sub,
        ast.Mult: operator.mul, ast.Div: operator.truediv,
    }

    def evaluate(node: ast.AST) -> float:
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return float(node.value)
        if isinstance(node, ast.Name) and node.id in ("pi", "e"):
            return getattr(math, node.id)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = evaluate(node.operand)
            return -value if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.BinOp) and type(node.op) in operations:
            return operations[type(node.op)](evaluate(node.left), evaluate(node.right))
        raise ValueError("calc permits numbers, pi/e, parentheses and + - * / only.")

    result = evaluate(tree.body)
    if not math.isfinite(result):
        raise ValueError("Calculation result must be finite.")
    return result


class QuickCheck(BaseModel):
    passed: bool
    reasons: list[str]


def quick_check(candidates: Iterable[Candidate | Mapping[str, object]]) -> list[QuickCheck]:
    results = []
    for candidate in candidates:
        try:
            data = candidate.model_dump() if isinstance(candidate, Candidate) else candidate
            Candidate.model_validate(data)
        except ValidationError as error:
            results.append(QuickCheck(
                passed=False, reasons=[
                    f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
                    for item in error.errors()
                ],
            ))
        else:
            results.append(QuickCheck(passed=True, reasons=[]))
    return results


def retrieve_designs(
    memory: "Memory", targets: "Targets", circuit_sha: str = "",
    *, provenance: str | None = None,
) -> list["StoredDesign"]:
    desired = targets.model_dump()

    def distance(design: "StoredDesign") -> float:
        return sum(abs(math.log(value / desired[name]))
                   for name, value in design.targets.model_dump().items())

    same_circuit = [
        design for design in memory.designs() if design.circuit_sha == circuit_sha
        and (provenance is None or design.provenance.get("simulation_provenance") == provenance)
    ]
    return sorted(same_circuit, key=distance)[:2]


def ask_engineer(
    question: str, *, yes: bool = False, read: Callable[[str], str] = input,
) -> str:
    print(question, flush=True)
    if yes:
        print("Engineer verdict: approve (--yes)", flush=True)
        return "approve"
    while True:
        try:
            answer = read("Type approve or reject: ").strip().lower()
        except EOFError as error:
            raise RuntimeError("No engineer input available; rerun interactively or use --yes.") from error
        if answer in ("approve", "reject"):
            return answer
        print("Invalid verdict: type approve or reject.", flush=True)


PAUSE_ACTIONS = ("keep", "drop", "pin", "stop")


def ask_pause(
    question: str, *, yes: bool = False, read: Callable[[str], str] = input,
) -> str:
    print(question, flush=True)
    if yes:
        print("Pause verdict: keep (--yes)", flush=True)
        return "keep"
    while True:
        try:
            answer = read("Type keep, drop, pin or stop: ").strip().lower()
        except EOFError as error:
            raise RuntimeError("No engineer input available; rerun interactively or use --yes.") from error
        if answer in PAUSE_ACTIONS:
            return answer
        print("Invalid choice: type keep, drop, pin or stop.", flush=True)
