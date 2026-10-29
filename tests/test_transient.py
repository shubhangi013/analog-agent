import math
import re
from pathlib import Path

import pytest

from agent.diagnostics import DEFAULT_CANDIDATE, circuit_body
from agent.memory import fingerprint
from agent.sim import Condition, parse_transient, render_netlist


def write_waveform(path: Path, *, clipped: bool = False, distortion: bool = False) -> None:
    rows = []
    for index in range(5001):
        time_s = index * 1e-9
        sine = math.sin(2 * math.pi * 1e6 * time_s)
        out = 0.02 + 1e-5 * sine if clipped else (
            0.9 + (0.05 if sine >= 0 else -0.05) if distortion else 0.9 + 0.05 * sine)
        rows.append(f"{time_s:.15g} {0.9+0.001*sine:.15g} {0.9-0.001*sine:.15g} {out:.15g}")
    path.write_text("\n".join(rows) + "\n")


@pytest.mark.parametrize("clipped,distortion,expected", [
    (False, False, False), (True, False, True), (False, True, True),
])
def test_transient_gate_uses_measured_rail_occupancy_and_sine_residual(
    tmp_path: Path, clipped: bool, distortion: bool, expected: bool,
) -> None:
    path = tmp_path / "waveform.tsv"
    write_waveform(path, clipped=clipped, distortion=distortion)
    metrics = parse_transient(path, 1.8)
    assert metrics.clipped_or_distorted is expected
    assert metrics.rail_fraction == (1 if clipped else 0)
    assert metrics.stimulus_amplitude_v == 0.002
    if not clipped and not distortion:
        assert metrics.min_v == pytest.approx(0.85)
        assert metrics.max_v == pytest.approx(0.95)
        assert metrics.sine_residual_ratio < 1e-4


@pytest.mark.parametrize("fault", ["truncated", "time_axis", "undersampled", "stimulus"])
def test_incomplete_or_changed_stimulus_waveforms_cannot_pass(tmp_path: Path, fault: str) -> None:
    path = tmp_path / "waveform.tsv"
    write_waveform(path)
    rows = path.read_text().splitlines()
    if fault == "truncated":
        rows = rows[:1000]
    elif fault == "time_axis":
        rows[20] = rows[19]
    elif fault == "undersampled":
        rows = rows[:2000] + rows[3000:]
    else:
        rows = [" ".join([row.split()[0], "0.9", "0.9", row.split()[3]]) for row in rows]
    path.write_text("\n".join(rows) + "\n")
    with pytest.raises(ValueError):
        parse_transient(path, 1.8)


def test_transient_fingerprint_is_distinct_and_repair_never_changes_the_stimulus() -> None:
    candidate, condition = DEFAULT_CANDIDATE, Condition()
    body = circuit_body()
    load = body + "\nRload out 0 100\n"
    repaired = body + "\nRload out 0 10000000\n"
    original = render_netlist(candidate, condition, "tran", load)
    changed = render_netlist(candidate, condition, "tran", repaired)
    assert original.replace("Rload out 0 100\n", "Rload out 0 10000000\n") == changed
    assert "SIN(" in original and "wrdata transient.tsv" in original
    hashes = {fingerprint(render_netlist(candidate, condition, analysis, body), b"model", "version", condition)
              for analysis in ("op", "ac", "tran")}
    assert len(hashes) == 3


def test_fixed_bias_counterfactual_bypasses_recalibration_and_changes_cache_key() -> None:
    candidate, condition = DEFAULT_CANDIDATE, Condition()
    body = circuit_body()
    original = render_netlist(candidate, condition, "tran", body)
    held = render_netlist(candidate, condition, "tran", body, fixed_bias_v=0.7123456789)
    assert "dc Vbias" not in held and "meas dc bias_voltage" not in held
    assert float(re.search(r"alter Vbias ([^\n]+)", held)[1]) == 0.7123456789
    assert "SIN(" in held and "wrdata transient.tsv" in held
    assert fingerprint(original, b"model", "version", condition) != fingerprint(
        held, b"model", "version", condition)
    with pytest.raises(ValueError, match="within the supply"):
        render_netlist(candidate, condition, fixed_bias_v=2)
