import cmath
import math
from pathlib import Path
import subprocess

from pydantic import ValidationError
import pytest

from agent import sim
from agent.sim import Candidate, Condition, parse_ac, parse_op, read_table, render_netlist


@pytest.fixture
def candidate() -> Candidate:
    return Candidate(
        W_in=10, L_in=1, W_load=5, L_load=1, W_tail=5, L_tail=1, I_bias=25
    )


def write_table(path: Path, rows: list[list[float]]) -> Path:
    path.write_text(
        "\n".join(" ".join(str(value) for value in row) for row in rows),
        encoding="utf-8",
    )
    return path


def test_single_pole_gain_ugf_phase(tmp_path: Path) -> None:
    rows = []
    for index in range(801):
        frequency = 10 ** (index / 100)
        value = 100 / (1 + 1j * frequency / 1000)
        rows.append([frequency, value.real, value.imag])
    result = parse_ac(write_table(tmp_path / "ac.tsv", rows), 45e-6)
    ugf = 1000 * math.sqrt(100 ** 2 - 1)
    assert result.gain_db == pytest.approx(40, abs=0.001)
    assert result.ugf_hz == pytest.approx(ugf, rel=0.001)
    assert result.pm_deg == pytest.approx(
        180 - math.degrees(math.atan(ugf / 1000)), abs=0.01
    )
    assert result.power_w == 45e-6


def test_unwrap_phase_across_minus_180(tmp_path: Path) -> None:
    rows = []
    for frequency, magnitude, phase in [(1, 10, -170), (10, 0.1, 170)]:
        value = cmath.rect(magnitude, math.radians(phase))
        rows.append([frequency, value.real, value.imag])
    result = parse_ac(write_table(tmp_path / "ac.tsv", rows), 1e-4)
    assert result.ugf_hz == pytest.approx(math.sqrt(10))
    assert result.pm_deg == pytest.approx(0)


@pytest.mark.parametrize("rows, message", [
    ([[1, 10, 0], [10, 2, 0]], "No downward"),
    ([[1, 0.5, 0], [10, 0.1, 0]], "not above unity"),
    ([[1, 10, 0], [10, 0, 0]], "Zero AC"),
    ([[10, 10, 0], [1, 0.1, 0]], "strictly increasing"),
    ([[0, 10, 0], [1, 0.1, 0]], "positive"),
])
def test_invalid_ac(tmp_path: Path, rows: list[list[float]], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_ac(write_table(tmp_path / "ac.tsv", rows), 45e-6)


@pytest.mark.parametrize("text", ["", "1 2", "1 nan 3", "1 inf 3", "bad 2 3"])
def test_invalid_tables(tmp_path: Path, text: str) -> None:
    path = tmp_path / "bad.tsv"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        read_table(path, 3)


def test_op_does_not_hide_reverse_bias(tmp_path: Path) -> None:
    row = [0, 1, 0.6, -25e-6, 25e-6]
    for _ in range(5):
        row.extend([-0.2, -0.1, -0.5, -0.4, -12.5e-6])
    bias, output, gate, supply, tail = parse_op(
        write_table(tmp_path / "op.tsv", [row])
    )
    assert len(bias) == 5
    assert bias["m3"].vds == -0.2
    assert bias["m3"].vgs == -0.5
    assert bias["m3"].vdsat == 0.1
    assert bias["m3"].vth == 0.4
    assert (output, gate, supply, tail) == (1, 0.6, 25e-6, 25e-6)


def test_template(candidate: Candidate) -> None:
    condition = Condition(name="hot_low", temperature_c=125, supply_v=1.62)
    text = render_netlist(candidate, condition)
    assert "{{" not in text
    assert ".temp 125" in text
    assert "Vdd vdd 0 1.62" in text
    assert "i(Vsense)=2.5e-05" in text
    assert sum(line.startswith(("M1 ", "M2 ", "M3 ", "M4 ", "M5 "))
               for line in text.splitlines()) == 5
    assert "ac dec" not in render_netlist(candidate, condition, "op")


@pytest.mark.parametrize("field,value", [
    ("I_bias", 1), ("L_in", 0.17), ("W_tail", 101), ("W_load", math.nan),
])
def test_candidate_ranges(candidate: Candidate, field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        Candidate.model_validate({**candidate.model_dump(), field: value})


def test_timeout_is_actionable(
    candidate: Candidate, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sim, "ROOT", tmp_path)
    monkeypatch.setattr(sim, "find_ngspice", lambda: "ngspice")
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "ptm180.lib").write_text("", encoding="utf-8")

    def timeout(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired("ngspice", 0.01)

    monkeypatch.setattr(sim.subprocess, "run", timeout)
    result = sim.simulate(candidate, netlist="test\n.end", timeout_s=0.01)
    assert result.status == "timeout"
    assert result.error is not None and "was killed" in result.error


def test_ngspice_error_even_with_zero_exit(
    candidate: Candidate, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sim, "ROOT", tmp_path)
    monkeypatch.setattr(sim, "find_ngspice", lambda: "ngspice")
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "ptm180.lib").write_text("", encoding="utf-8")

    def failed_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        log = Path(str(kwargs["cwd"])) / "ngspice.log"
        log.write_text("Error: DC solution failed to converge\n", encoding="utf-8")
        return subprocess.CompletedProcess(["ngspice"], 0)

    monkeypatch.setattr(sim.subprocess, "run", failed_run)
    result = sim.simulate(candidate, netlist="test\n.end")
    assert result.status == "error"
    assert result.error is not None and "converge" in result.error


@pytest.mark.parametrize("fixed,measured_voltage,status,message", [
    (False, 0.6, "error", "calibration missed"),
    (True, 0.6, "ok", None),
    (True, 0.7, "error", "Fixed-bias voltage"),
])
def test_bias_validation_distinguishes_calibrated_current_from_held_voltage(
    candidate: Candidate, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    fixed: bool, measured_voltage: float, status: str, message: str | None,
) -> None:
    circuit = render_netlist(candidate, Condition(), "op", fixed_bias_v=0.6 if fixed else None)
    monkeypatch.setattr(sim, "ROOT", tmp_path)
    monkeypatch.setattr(sim, "parse_op", lambda path: ({}, 0.8, measured_voltage, 50e-6, 50e-6))
    monkeypatch.setattr(sim, "circuit_nodes", lambda text: ["out", "bias"])
    monkeypatch.setattr(sim, "read_table", lambda path, columns: [[0, 0.8, measured_voltage]])

    def clean_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        (Path(str(kwargs["cwd"])) / "ngspice.log").write_text("Completed\n", encoding="utf-8")
        return subprocess.CompletedProcess(["ngspice"], 0)

    monkeypatch.setattr(sim.subprocess, "run", clean_run)
    result = sim.simulate(
        candidate, analysis="op", netlist=circuit, model_contents=b"model", executable="fake")
    assert result.status == status
    if message:
        assert message in result.error
    else:
        assert result.tail_current_a == 50e-6  # Held voltage does not promise the requested current.
        assert result.bias_voltage_v == 0.6
