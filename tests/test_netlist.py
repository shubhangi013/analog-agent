from pathlib import Path
from hashlib import sha256
import re

import pytest

from agent.netlist import SIZED, load_circuit
from agent.sim import ROOT, Candidate, Condition, render_netlist


TEMPLATE = (ROOT / "circuits" / "ota5t.cir.j2").read_text(encoding="utf-8")
HEAD = TEMPLATE[: TEMPLATE.index("\n.control")]
CANDIDATE = Candidate(W_in=20, L_in=0.5, W_load=10, L_load=0.5, W_tail=10, L_tail=0.5, I_bias=25)
TYPICAL = Condition(name="typical", temperature_c=27, supply_v=1.8)


def annotations(names: tuple[str, ...] = SIZED) -> str:
    return "".join(f"* @size {name}\n" for name in names)


def valid_body() -> str:
    return annotations() + HEAD + "\n"


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "engineer.cir"
    path.write_text(text, encoding="utf-8")
    return path


def test_valid_body_is_accepted_and_hashed(tmp_path: Path) -> None:
    body = valid_body()
    circuit = load_circuit(write(tmp_path, body))
    assert circuit.body == body
    assert circuit.sha == sha256(body.encode("utf-8")).hexdigest()


def test_template_body_renders_like_the_default() -> None:
    rendered_default = render_netlist(CANDIDATE, TYPICAL, "ac")
    assert render_netlist(CANDIDATE, TYPICAL, "ac", circuit=HEAD) == rendered_default
    assert render_netlist(CANDIDATE, TYPICAL, "op", circuit=HEAD) == render_netlist(
        CANDIDATE, TYPICAL, "op",
    )


def test_spaceless_placeholders_are_accepted_and_rendered(tmp_path: Path) -> None:
    body = valid_body().replace("{{ W_in }}", "{{W_in}}")
    circuit = load_circuit(write(tmp_path, body))
    rendered = render_netlist(CANDIDATE, TYPICAL, "ac", circuit=circuit.body)
    assert "W=20u" in rendered
    assert "{{" not in rendered


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (valid_body() + ".control\n", "remove .control, .endc and .end"),
        (valid_body() + ".end\n", "remove .control, .endc and .end"),
        (annotations(tuple(name for name in SIZED if name != "L_in")) + HEAD,
         "missing '* @size NAME' annotation for: L_in"),
        (valid_body() + "* @size W_in\n", "duplicate @size annotations: W_in"),
        (valid_body() + "* @size I_bias\n", "unknown @size names (not sizing fields): I_bias"),
        (valid_body().replace("{{ W_in }}", "{{ W_in }} {{ foo }}"),
         "unknown placeholders: {{ foo }}"),
        (valid_body().replace("{{ L_tail }}", "0.5"),
         "sized parameter L_tail is annotated but never used"),
        (valid_body().replace("{{ temperature_c }}", "27"),
         "the body must use {{ temperature_c }}"),
        (valid_body().replace("M5 tail_d bias 0 0 NMOS", "X5 tail_d bias 0 0 NMOS"),
         "missing required elements: M5"),
        (valid_body().replace("tail_d", "td"), "missing required nodes: tail_d"),
    ],
)
def test_invalid_body_is_rejected(tmp_path: Path, body: str, message: str) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        load_circuit(write(tmp_path, body))


def test_every_problem_is_reported_together(tmp_path: Path) -> None:
    body = valid_body().replace("{{ L_tail }}", "0.5") + ".control\n"
    with pytest.raises(ValueError) as error:
        load_circuit(write(tmp_path, body))
    text = str(error.value)
    assert "Netlist engineer.cir is not usable:" in text
    assert "remove .control" in text
    assert "L_tail is annotated but never used" in text


def test_missing_file_is_an_oserror(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        load_circuit(tmp_path / "absent.cir")
