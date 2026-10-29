"""Deterministic intake for the supported OTA specification vocabulary."""

import re
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agent.verifier import Targets


Objective = Literal["feasibility", "minimize_power", "maximize_gain", "maximize_bandwidth"]


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str = Field(min_length=1, max_length=2000)
    objective: Objective = "feasibility"
    topology: Literal["ota5t"] = "ota5t"
    targets: Targets
    assumptions: list[str] = Field(default_factory=list)


def parse_request(goal: str, defaults: Targets) -> Request:
    if not goal.strip():
        raise ValueError("Provide a nonempty circuit specification.")
    if re.search(r"\b(?:opamp|op-amp|two.stage|cascode|folded|bandgap|oscillator)\b", goal, re.I):
        raise ValueError("Only the differential five-transistor OTA topology is supported.")
    if re.search(r"\b(?:noise|offset|settling|slew|area|rail.to.rail)\b", goal, re.I):
        raise ValueError("Only gain, UGF, phase margin and power constraints are supported.")
    normalized = goal
    normalized = re.sub(
        r"\b(at least|minimum)\s+(\d+(?:\.\d+)?)\s*(dB)\s+gain\b",
        r"gain \1 \2 \3", normalized, flags=re.I,
    )
    normalized = re.sub(
        r"\b(\d+(?:\.\d+)?)\s*([kmg]?Hz)\s+(?:bandwidth|ugf)\b",
        r"bandwidth \1 \2", normalized, flags=re.I,
    )
    normalized = re.sub(
        r"\b(\d+(?:\.\d+)?)\s*(deg(?:rees)?|degree)\s+(?:phase margin|pm)\b",
        r"phase margin \1 \2", normalized, flags=re.I,
    )
    normalized = re.sub(
        r"\b(\d+(?:\.\d+)?)\s*([mun]?W)\s+(?:max(?:imum)?|at most)\b",
        r"power maximum \1 \2", normalized, flags=re.I,
    )
    patterns = {
        "gain_db": r"\bgain\s*(>=|>|at least|minimum|of)?\s*(\d+(?:\.\d+)?)\s*(dB)\b",
        "ugf_hz": r"\b(?:ugf|bandwidth|unity.gain frequency)\s*(>=|>|at least|minimum|of)?\s*(\d+(?:\.\d+)?)\s*([kmg]?Hz)\b",
        "pm_deg": r"\b(?:phase margin|pm)\s*(>=|>|at least|minimum|of)?\s*(\d+(?:\.\d+)?)\s*(deg(?:rees)?|degree)\b",
        "power_w": r"\bpower\s*(<=|<|at most|maximum|under|below|of)?\s*(\d+(?:\.\d+)?)\s*([mun]?W)\b",
    }
    values = defaults.model_dump()
    assumptions = []
    for name, pattern in patterns.items():
        matches = list(re.finditer(pattern, normalized, re.I))
        if len(matches) > 1:
            raise ValueError(f"Specify {name} once; repeated constraints need clarification.")
        if not matches:
            assumptions.append(f"No {name} target supplied; using {values[name]:g} in SI units.")
            continue
        comparison, number, unit = matches[0].groups()
        if comparison in ("<", ">"):
            raise ValueError("Use inclusive >= or <= constraints; strict inequalities are unsupported.")
        unit = unit.lower()
        scale = ({"khz": 1e3, "mhz": 1e6, "ghz": 1e9, "mw": 1e-3,
                  "uw": 1e-6, "nw": 1e-9}).get(unit, 1)
        values[name] = float(Decimal(number) * Decimal(str(scale)))
    # Do not silently drop a numeric constraint outside the supported grammar.
    remainder = normalized
    for pattern in patterns.values():
        remainder = re.sub(pattern, "", remainder, flags=re.I)
    if re.search(r"\d|[<>]", remainder):
        raise ValueError("Unrecognized numeric constraint. Use gain >= N dB, UGF >= N MHz, "
                         "phase margin >= N deg, and power <= N uW.")
    objectives = []
    for objective, pattern in (
        ("minimize_power", r"\b(?:minimi[sz]e|lowest|reduce)\s+(?:the\s+)?power\b"),
        ("maximize_gain", r"\b(?:maximi[sz]e|highest|increase)\s+(?:the\s+)?gain\b"),
        ("maximize_bandwidth", r"\b(?:maximi[sz]e|highest|increase)\s+(?:the\s+)?(?:bandwidth|ugf)\b"),
    ):
        if re.search(pattern, normalized, re.I):
            objectives.append(objective)
    if len(objectives) > 1:
        raise ValueError("Choose one optimization objective.")
    if re.search(r"\b(?:minimi[sz]e|maximi[sz]e)\b", normalized, re.I) and not objectives:
        raise ValueError("Supported objectives: minimize power, maximize gain, maximize bandwidth.")
    return Request(
        goal=goal, targets=Targets(**values),
        objective=objectives[0] if objectives else "feasibility", assumptions=assumptions,
    )
