"""Engineer-supplied netlists for the fixed five-transistor OTA topology."""

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re

from agent.sim import Candidate


SIZED = tuple(name for name in Candidate.model_fields if name != "I_bias")
CONDITION_KEYS = ("temperature_c", "supply_v", "common_mode_v", "bias_a")
REQUIRED_ELEMENTS = ("M1", "M2", "M3", "M4", "M5", "Vdd", "Vip", "Vin", "Vbias", "Vsense", "Cload")
REQUIRED_NODES = ("out", "inp", "inn", "mirror", "tail", "tail_d", "bias", "vdd")
FORBIDDEN_CARDS = re.compile(r"^\s*\.(control|endc|end)\b", re.IGNORECASE | re.MULTILINE)
SIZE_ANNOTATION = re.compile(r"^\s*\*\s*@size\s+(\w+)\s*$", re.MULTILINE)
PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")


@dataclass(frozen=True)
class Circuit:
    path: str
    body: str
    sha: str


def load_circuit(path: Path) -> Circuit:
    """Validate a netlist body; raise ValueError listing every problem found."""
    body = path.read_text(encoding="utf-8")
    problems: list[str] = []
    if FORBIDDEN_CARDS.search(body):
        problems.append("remove .control, .endc and .end; the tool adds the measurement block.")

    annotated = SIZE_ANNOTATION.findall(body)
    duplicates = sorted({name for name in annotated if annotated.count(name) > 1})
    if duplicates:
        problems.append(f"duplicate @size annotations: {', '.join(duplicates)}.")
    missing = [name for name in SIZED if name not in annotated]
    unknown = [name for name in annotated if name not in SIZED]
    if missing:
        problems.append(f"missing '* @size NAME' annotation for: {', '.join(missing)}.")
    if unknown:
        problems.append(f"unknown @size names (not sizing fields): {', '.join(unknown)}.")

    used = set(PLACEHOLDER.findall(body))
    allowed = set(SIZED) | set(CONDITION_KEYS)
    bad = sorted(used - allowed)
    if bad:
        problems.append(f"unknown placeholders: {', '.join('{{ ' + name + ' }}' for name in bad)}.")
    for name in SIZED:
        if name in annotated and name not in used:
            problems.append(f"sized parameter {name} is annotated but never used as {{{{ {name} }}}}.")
    for name in ("temperature_c", "supply_v"):
        if name not in used:
            problems.append(f"the body must use {{{{ {name} }}}} so the corner conditions apply.")

    tokens = set()
    elements = set()
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("*"):
            continue
        words = stripped.split()
        elements.add(words[0].upper())
        tokens.update(word.lower() for word in words)
    absent = [name for name in REQUIRED_ELEMENTS if name.upper() not in elements]
    if absent:
        problems.append(f"missing required elements: {', '.join(absent)}.")
    absent_nodes = [name for name in REQUIRED_NODES if name not in tokens]
    if absent_nodes:
        problems.append(f"missing required nodes: {', '.join(absent_nodes)}.")

    if problems:
        raise ValueError(f"Netlist {path.name} is not usable: " + " ".join(problems))
    return Circuit(path=str(path), body=body, sha=sha256(body.encode("utf-8")).hexdigest())
