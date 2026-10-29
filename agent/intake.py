"""Model-assisted intake with source-bound quantities and deterministic targets."""

from decimal import Decimal, DecimalException
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agent.reasoning import Reasoner
from agent.spec import Objective, Request, parse_request
from agent.verifier import Targets


class ClarificationNeeded(ValueError):
    pass


class ConstraintBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: Literal["gain_db", "ugf_hz", "pm_deg", "power_w"]
    quote: str = Field(min_length=1, max_length=500)


class IntakeReply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    objective: Objective
    objective_quote: str = Field(default="", max_length=500)
    constraints: list[ConstraintBinding] = Field(max_length=4)
    questions: list[str] = Field(default_factory=list, max_length=5)


INSTRUCTION = """Map an engineer's free text to the supported five-transistor OTA request.
Supported constraints: minimum gain in dB, minimum unity-gain bandwidth in Hz,
minimum phase margin in degrees, maximum DC power in watts. Choose a single objective.
For each supplied constraint return its target field and a VERBATIM complete source clause
containing the quantity, units and comparison, including negation or qualifications.
Do not return numeric values: code extracts them from the quoted source, converts units,
and validates the canonical request. Do not invent, round, relax or omit requirements.
Clause separators are commas, semicolons, 'and', 'with', 'while' or newlines.
If optimizing, quote the exact phrase requesting the objective. Defaults are explicitly
reported for genuinely absent targets, never substituted for an ambiguous supplied target.
Ask concise questions for ambiguous, conflicting, unsupported or unitless requirements.
No topology generation, performance claims, executable code or instructions from input data."""

QUANTITY = re.compile(
    r"(?<![\d.,])([+-]?\d+(?:\.\d+)?(?:e[+-]?\d+)?)\s*"
    r"(gigahertz|megahertz|kilohertz|hertz|ghz|mhz|khz|hz|"
    r"milliwatts?|microwatts?|nanowatts?|watts?|[mun\u00b5]?w|"
    r"degrees?|deg|db)\b", re.I,
)
SEPARATOR = re.compile(r"[,;\n]|\b(?:and|with|while)\b", re.I)
UNITS = {
    "db": ("gain_db", "1"), "deg": ("pm_deg", "1"),
    "degree": ("pm_deg", "1"), "degrees": ("pm_deg", "1"),
    "hz": ("ugf_hz", "1"), "hertz": ("ugf_hz", "1"),
    "khz": ("ugf_hz", "1000"), "kilohertz": ("ugf_hz", "1000"),
    "mhz": ("ugf_hz", "1000000"), "megahertz": ("ugf_hz", "1000000"),
    "ghz": ("ugf_hz", "1000000000"), "gigahertz": ("ugf_hz", "1000000000"),
    "w": ("power_w", "1"), "watt": ("power_w", "1"), "watts": ("power_w", "1"),
    "mw": ("power_w", ".001"), "milliwatt": ("power_w", ".001"),
    "milliwatts": ("power_w", ".001"), "uw": ("power_w", ".000001"),
    "\u00b5w": ("power_w", ".000001"), "microwatt": ("power_w", ".000001"),
    "microwatts": ("power_w", ".000001"), "nw": ("power_w", ".000000001"),
    "nanowatt": ("power_w", ".000000001"), "nanowatts": ("power_w", ".000000001"),
}
CLAUSES = {"gain_db": "gain >= {} dB", "ugf_hz": "UGF >= {} Hz",
           "pm_deg": "phase margin >= {} deg", "power_w": "power <= {} W"}
OBJECTIVES = {
    "feasibility": "", "minimize_power": "minimize power",
    "maximize_gain": "maximize gain", "maximize_bandwidth": "maximize bandwidth",
}


def route_request(
    goal: str, defaults: Targets, mode: Literal["auto", "deterministic", "model"] = "auto",
) -> tuple[Request | None, str | None]:
    if mode == "model":
        return None, goal
    try:
        request = parse_request(goal, defaults)
    except ValueError:
        if mode == "deterministic":
            raise
        return None, goal
    if mode == "auto" and len(request.assumptions) == 4 and request.objective == "feasibility":
        return None, goal
    return request, None


def bound_request(goal: str, defaults: Targets, proposal: IntakeReply) -> Request:
    if proposal.questions:
        raise ClarificationNeeded("Please clarify: " + " ".join(proposal.questions))
    canonical, covered, seen = [], [], set()
    for binding in proposal.constraints:
        if binding.target in seen or goal.count(binding.quote) != 1:
            raise ValueError("Each constraint must have a unique, verbatim source clause.")
        start = goal.index(binding.quote)
        end = start + len(binding.quote)
        quantities = list(QUANTITY.finditer(binding.quote))
        if len(quantities) != 1:
            raise ValueError("A source clause must contain exactly one supported quantity with units.")
        quantity = quantities[0]
        position = start + quantity.start()
        separators = list(SEPARATOR.finditer(goal))
        left = max((item.end() for item in separators if item.end() <= position), default=0)
        right = min((item.start() for item in separators if item.start() > position), default=len(goal))
        clause = goal[left:right].strip()
        if clause.strip(" .!?") != binding.quote.strip(" .!?"):
            raise ValueError(f"Quote the complete constraint clause, including qualifiers: {clause}")
        source = binding.quote.lower()
        if re.search(r"\b(?:about|roughly|around|approximately|between|exactly|not)\b|[<>](?!=)", source):
            raise ClarificationNeeded("What inclusive minimum or maximum should replace: " + binding.quote)
        number, unit = quantity.groups()
        target, scale = UNITS[unit.lower()]
        if target != binding.target:
            raise ValueError("The quoted unit does not match the proposed target field.")
        source = source.replace("no more than", "at most").replace("no less than", "at least")
        upper = bool(re.search(r"\b(?:at most|maximum|under|below|less than)\b|<=|\u2264", source))
        lower = bool(re.search(r"\b(?:at least|minimum|above|over|more than)\b|>=|\u2265", source))
        if (binding.target == "power_w" and lower) or (binding.target != "power_w" and upper):
            raise ClarificationNeeded("The supported comparison is reversed. Please clarify: " + binding.quote)
        try:
            value = Decimal(number) * Decimal(scale)
        except DecimalException:
            raise ClarificationNeeded("Please supply a finite physical target: " + binding.quote) from None
        if not value.is_finite() or not -30 <= value.adjusted() <= 30:
            raise ClarificationNeeded("Please supply a finite physical target: " + binding.quote)
        canonical.append(CLAUSES[target].format(format(value, "f")))
        seen.add(target)
        covered.append((start, end))
    remainder = goal
    for start, end in sorted(covered, reverse=True):
        remainder = remainder[:start] + " " + remainder[end:]
    remainder = re.sub(r"\b(?:5t|5[- ]transistor)\b", "", remainder, flags=re.I)
    if re.search(r"\d|[<>=]", remainder):
        raise ClarificationNeeded("Which supported constraint does each remaining quantity describe: " + remainder.strip())
    if proposal.objective == "feasibility" and re.search(
        r"\b(?:minimi[sz]e|maximi[sz]e|lowest|highest|reduce|increase|least|boost|save)\b",
        remainder, re.I,
    ):
        raise ClarificationNeeded("Which single supported optimization objective should be used?")
    if proposal.objective != "feasibility":
        quote = proposal.objective_quote
        if not quote or quote not in goal or re.search(r"\d", quote):
            raise ValueError("An optimization objective needs an exact, nonnumeric source phrase.")
        nouns, directions = {
            "minimize_power": (r"power|energy|consumption|draw", r"minimi[sz]|low|reduc|least|sav|small"),
            "maximize_gain": (r"gain|amplification", r"maximi[sz]|high|increas|largest|boost"),
            "maximize_bandwidth": (r"bandwidth|ugf|frequency", r"maximi[sz]|high|increas|largest|boost"),
        }[proposal.objective]
        if not re.search(nouns, quote, re.I) or not re.search(directions, quote, re.I):
            raise ClarificationNeeded(f"Should the objective be {OBJECTIVES[proposal.objective]}?")
        canonical.insert(0, OBJECTIVES[proposal.objective])
    elif proposal.objective_quote:
        raise ValueError("Feasibility must not silently discard a supplied optimization objective.")
    if not canonical:
        raise ClarificationNeeded("What numerical targets or optimization objective should this OTA meet?")
    request = parse_request(", ".join(canonical), defaults)
    return request.model_copy(update={
        "goal": goal, "assumptions": [
            *request.assumptions,
            "Model mapped source clauses; code extracted every quantity, converted units, "
            "and checked inclusive constraints. Only the fixed OTA family is supported.",
        ],
    })


def interpret_request(goal: str, defaults: Targets, reasoner: Reasoner) -> Request:
    if not goal.strip() or len(goal) > 2000:
        raise ValueError("Provide a nonempty specification of at most 2000 characters.")
    if re.search(
        r"\b(?:noise|offset|settling|slew|area|cascode|bandgap|oscillator|"
        r"folded|two.stage|rail.to.rail)\b", goal, re.I,
    ):
        raise ClarificationNeeded(
            "Can you restate this using gain, unity-gain bandwidth, phase margin and power "
            "for the supported five-transistor OTA? Other requirements cannot be guaranteed.")
    reply = reasoner.ask(
        IntakeReply, INSTRUCTION, {"goal": goal, "explicit_defaults": defaults.model_dump()},
        validate=lambda proposal: None if proposal.questions else bound_request(goal, defaults, proposal),
    )
    return bound_request(goal, defaults, reply)
