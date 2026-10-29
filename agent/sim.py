"""Render and simulate the fixed 5T OTA; keep raw output outside model context."""

import cmath
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from agent.smoke import ROOT, find_ngspice


Analysis = Literal["op", "ac", "tran"]
TRANSIENT_FREQUENCY_HZ = 1e6
TRANSIENT_AMPLITUDE_V = 0.002
TRANSIENT_START_S = 1e-6
TRANSIENT_STOP_S = 5e-6


class Candidate(BaseModel):
    """Dimensions in micrometres, bias current in microamps."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    W_in: float = Field(ge=1, le=100)
    L_in: float = Field(ge=0.18, le=2)
    W_load: float = Field(ge=1, le=100)
    L_load: float = Field(ge=0.18, le=2)
    W_tail: float = Field(ge=1, le=100)
    L_tail: float = Field(ge=0.18, le=2)
    I_bias: float = Field(ge=5, le=200)


class Condition(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    name: str = "typical"
    temperature_c: float = Field(default=27, ge=-40, le=125)
    supply_v: float = Field(default=1.8, ge=1.62, le=1.98)


class BiasPoint(BaseModel):
    """PMOS voltages are polarity-normalized (Vsd, Vsg, |Vth|)."""

    vds: float
    vdsat: float
    vgs: float
    vth: float
    id_a: float


class Metrics(BaseModel):
    gain_db: float
    ugf_hz: float
    pm_deg: float
    power_w: float


class TransientMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    min_v: float
    max_v: float
    mean_v: float
    rail_fraction: float = Field(ge=0, le=1)
    sine_residual_ratio: float | None = Field(default=None, ge=0)
    clipped_or_distorted: bool
    stimulus_frequency_hz: float = TRANSIENT_FREQUENCY_HZ
    stimulus_amplitude_v: float = TRANSIENT_AMPLITUDE_V


class SimulationResult(BaseModel):
    status: Literal["ok", "error", "timeout"]
    condition: Condition
    analysis: Analysis
    metrics: Metrics | None = None
    transient: TransientMetrics | None = None
    bias: dict[str, BiasPoint] = Field(default_factory=dict)
    output_v: float | None = None
    bias_voltage_v: float | None = None
    tail_current_a: float | None = None
    dc_power_w: float | None = None
    nodes: dict[str, float] = Field(default_factory=dict)
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)
    run_dir: str


def ngspice_version(executable: str) -> str:
    process = subprocess.run(
        [executable, "-v"], capture_output=True, text=True, timeout=15, check=False
    )
    output = (process.stdout + process.stderr).strip()
    if process.returncode != 0 or not re.search(r"ngspice-\d", output, re.IGNORECASE):
        raise RuntimeError(f"Could not identify ngspice version: {output[:500]}")
    return output


def render_netlist(
    candidate: Candidate, condition: Condition, analysis: Analysis = "ac",
    circuit: str | None = None, *, fixed_bias_v: float | None = None,
) -> str:
    template = (ROOT / "circuits" / "ota5t.cir.j2").read_text(encoding="utf-8")
    if circuit is not None:
        # Engineer body replaces the template's devices; the measurement block is kept.
        template = circuit.rstrip("\n") + "\n" + template[template.index("\n.control") + 1:]
    if analysis == "tran":
        for source, node, amplitude in (
            ("Vip", "inp", TRANSIENT_AMPLITUDE_V / 2),
            ("Vin", "inn", -TRANSIENT_AMPLITUDE_V / 2),
        ):
            template, count = re.subn(
                rf"(?im)^{source}\s+{node}\s+0\s+[^\n]+",
                f"{source} {node} 0 DC {{{{ common_mode_v }}}} "
                f"SIN({{{{ common_mode_v }}}} {amplitude:.12g} {TRANSIENT_FREQUENCY_HZ:.12g})",
                template,
            )
            if count != 1:
                raise ValueError("Transient adapter requires Vip/inp and Vin/inn voltage sources.")
    values = {
        **{name: f"{value:.12g}" for name, value in candidate.model_dump().items()},
        "temperature_c": f"{condition.temperature_c:.12g}",
        "supply_v": f"{condition.supply_v:.12g}",
        "common_mode_v": f"{condition.supply_v / 2:.12g}",
        "bias_a": f"{candidate.I_bias * 1e-6:.12g}",
        "node_commands": "wrdata nodes.tsv " + " ".join(
            f"v({node})" for node in circuit_nodes(template)
        ),
        "ac_commands": (
            "ac dec 100 1 10G\nwrdata ac.tsv v(out)" if analysis == "ac" else
            "tran 1n 5u\nwrdata transient.tsv v(inp) v(inn) v(out)" if analysis == "tran" else ""
        ),
    }
    template = re.sub(
        r"\{\{\s*(\w+)\s*\}\}", lambda match: values.get(match.group(1), match.group(0)), template,
    )
    if "{{" in template:
        raise ValueError("Unresolved circuit template placeholder.")
    if fixed_bias_v is not None:
        if not math.isfinite(fixed_bias_v) or not 0 <= fixed_bias_v <= condition.supply_v:
            raise ValueError("Fixed bias voltage must be finite and within the supply rails.")
        template, count = re.subn(
            r"(?m)^dc Vbias[^\n]*\nmeas dc bias_voltage[^\n]*\nalter Vbias \$&bias_voltage$",
            f"alter Vbias {fixed_bias_v:.17g}", template,
        )
        if count != 1:
            raise ValueError("Cannot replace the adapter's bias-calibration block.")
    return template


def circuit_nodes(text: str) -> list[str]:
    nodes: set[str] = set()
    for line in text.split(".control", 1)[0].splitlines()[1:]:
        words = line.split()
        if not words or words[0].startswith(("*", ".")):
            continue
        if words[0][0].lower() not in ("m", "v", "i", "r", "c", "l"):
            continue
        count = 4 if words[0][0].lower() == "m" else 2
        if len(words) < count + 2:
            continue
        for node in words[1:1 + count]:
            if not re.fullmatch(r"[A-Za-z0-9_]+", node):
                raise ValueError(f"Unsupported node name for measurement: {node!r}.")
            if node != "0":
                nodes.add(node.lower())
    return sorted(nodes)


def read_table(path: Path, columns: int) -> list[list[float]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = [float(value) for value in line.split()]
        if len(row) != columns or not all(math.isfinite(value) for value in row):
            raise ValueError(f"{path.name}: expected {columns} finite columns.")
        rows.append(row)
    if not rows:
        raise ValueError(f"{path.name}: simulator produced no data.")
    return rows


def parse_op(path: Path) -> tuple[dict[str, BiasPoint], float, float, float, float]:
    rows = read_table(path, 30)
    if len(rows) != 1:
        raise ValueError("Expected exactly one DC operating point.")
    row = rows[0]
    bias = {}
    for index, name in enumerate(("m1", "m2", "m3", "m4", "m5")):
        vds, vdsat, vgs, vth, current = row[5 + index * 5:10 + index * 5]
        bias[name] = BiasPoint(
            vds=vds,
            vdsat=abs(vdsat),
            vgs=vgs,
            vth=abs(vth),
            id_a=abs(current),
        )
    return bias, row[1], row[2], -row[3], row[4]


def parse_ac(path: Path, power_w: float) -> Metrics:
    rows = read_table(path, 3)
    if len(rows) < 2 or any(
        row[0] <= 0 or (index > 0 and row[0] <= rows[index - 1][0])
        for index, row in enumerate(rows)
    ):
        raise ValueError("AC frequencies must be positive and strictly increasing.")
    gains = []
    phases = []
    for _, real, imaginary in rows:
        value = complex(real, imaginary)
        if abs(value) == 0:
            raise ValueError("Zero AC magnitude prevents gain/phase extraction.")
        gains.append(20 * math.log10(abs(value)))
        # Complex wrdata values avoid ngspice's configurable phase display units.
        phase = math.degrees(cmath.phase(value))
        if phases:
            phase = phases[-1] + (phase - phases[-1] + 180) % 360 - 180
        phases.append(phase)
    if gains[0] <= 0:
        raise ValueError("Low-frequency gain is not above unity; UGF is undefined.")
    for index in range(1, len(rows)):
        if gains[index - 1] > 0 >= gains[index]:
            fraction = gains[index - 1] / (gains[index - 1] - gains[index])
            log_f = math.log10(rows[index - 1][0]) + fraction * math.log10(
                rows[index][0] / rows[index - 1][0]
            )
            phase = phases[index - 1] + fraction * (phases[index] - phases[index - 1])
            return Metrics(
                gain_db=gains[0], ugf_hz=10 ** log_f, pm_deg=180 + phase,
                power_w=power_w,
            )
    raise ValueError("No downward unity-gain crossing up to 10 GHz; extend the AC sweep.")


def parse_transient(path: Path, supply_v: float) -> TransientMetrics:
    rows = read_table(path, 4)
    if len(rows) < 500 or rows[0][0] > TRANSIENT_START_S or not math.isclose(
        rows[-1][0], TRANSIENT_STOP_S, rel_tol=1e-5,
    ) or any(rows[i][0] <= rows[i - 1][0] for i in range(1, len(rows))):
        raise ValueError("Transient waveform is incomplete or its time axis is invalid.")
    if max(rows[i][0] - rows[i - 1][0] for i in range(1, len(rows))) > 20e-9:
        raise ValueError("Transient waveform is undersampled.")
    samples, times, position = [], [], 0
    for index in range(2048):
        time_s = TRANSIENT_START_S + index * (TRANSIENT_STOP_S - TRANSIENT_START_S) / 2048
        while rows[position + 1][0] < time_s:
            position += 1
        left, right = rows[position], rows[position + 1]
        fraction = (time_s - left[0]) / (right[0] - left[0])
        samples.append(left[3] + fraction * (right[3] - left[3]))
        times.append(time_s)
        input_v = left[1] - left[2] + fraction * (
            right[1] - right[2] - left[1] + left[2])
        expected = TRANSIENT_AMPLITUDE_V * math.sin(2 * math.pi * TRANSIENT_FREQUENCY_HZ * time_s)
        if not math.isclose(input_v, expected, abs_tol=1e-6):
            raise ValueError("Transient stimulus differs from the fixed diagnostic contract.")
    mean = sum(samples) / len(samples)
    sine = [math.sin(2 * math.pi * TRANSIENT_FREQUENCY_HZ * t) for t in times]
    cosine = [math.cos(2 * math.pi * TRANSIENT_FREQUENCY_HZ * t) for t in times]
    a = 2 * sum((v - mean) * s for v, s in zip(samples, sine)) / len(samples)
    b = 2 * sum((v - mean) * c for v, c in zip(samples, cosine)) / len(samples)
    variance = sum((v - mean) ** 2 for v in samples)
    ratio = (math.sqrt(sum((v - mean - a * s - b * c) ** 2
                          for v, s, c in zip(samples, sine, cosine)) / variance)
             if variance > 1e-24 else None)
    rail_fraction = sum(v <= supply_v * 0.05 or v >= supply_v * 0.95
                        for v in samples) / len(samples)
    return TransientMetrics(
        min_v=min(samples), max_v=max(samples), mean_v=mean, rail_fraction=rail_fraction,
        sine_residual_ratio=ratio,
        clipped_or_distorted=(rail_fraction > 0.05 or ratio is None or ratio > 0.1),
    )


def simulate(
    candidate: Candidate,
    condition: Condition | None = None,
    *,
    analysis: Analysis = "ac",
    timeout_s: float = 30,
    netlist: str | None = None,
    model_contents: bytes | None = None,
    executable: str | None = None,
) -> SimulationResult:
    condition = condition if condition is not None else Condition()
    run_dir = ROOT / "runs" / "simulations" / uuid4().hex
    result = SimulationResult(
        status="error", condition=condition, analysis=analysis, run_dir=str(run_dir)
    )
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        result.error = "Simulation timeout must be positive and finite."
        return result
    try:
        executable = executable if executable is not None else find_ngspice()
        run_dir.mkdir(parents=True)
        if model_contents is None:
            shutil.copyfile(ROOT / "models" / "ptm180.lib", run_dir / "ptm180.lib")
        else:
            (run_dir / "ptm180.lib").write_bytes(model_contents)
        circuit = netlist if netlist is not None else render_netlist(
            candidate, condition, analysis
        )
        (run_dir / "candidate.cir").write_text(circuit, encoding="utf-8")
        log_path = run_dir / "ngspice.log"
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.run(
                [executable, "-b", "candidate.cir"], cwd=run_dir,
                stdout=log, stderr=subprocess.STDOUT, timeout=timeout_s, check=False,
            )
        output = log_path.read_text(encoding="utf-8", errors="replace")
        result.warnings = [
            line.strip() for line in output.splitlines()
            if "warning" in line.lower() or "unrecognized parameter" in line.lower()
        ]
        errors = [
            line.strip() for line in output.splitlines()
            if re.search(
                r"error|failed|not converg|timestep too small|singular matrix|aborted",
                line, re.IGNORECASE,
            )
        ]
        if process.returncode != 0 or errors:
            detail = "; ".join(errors[:4]) or f"exit code {process.returncode}"
            raise ValueError(
                f"ngspice failed: {detail}. Check netlist, models and DC bias; "
                f"see {log_path}"
            )
        bias, output_v, bias_voltage, supply_current, tail_current = parse_op(
            run_dir / "op.tsv"
        )
        controls = re.findall(r"^alter[ \t]+Vbias[ \t]+(\S+)[ \t]*$", circuit, re.MULTILINE | re.IGNORECASE)
        if controls == ["$&bias_voltage"]:
            if not math.isclose(tail_current, candidate.I_bias * 1e-6, rel_tol=0.01):
                raise ValueError(
                    "Tail-bias calibration missed I_bias by more than 1%; inspect DC "
                    "headroom or reduce the gate-bias sweep step."
                )
        elif len(controls) == 1:
            held_voltage = float(controls[0])
            if (not math.isfinite(held_voltage) or not 0 <= held_voltage <= condition.supply_v
                    or not math.isclose(bias_voltage, held_voltage, rel_tol=0, abs_tol=1e-12)):
                raise ValueError("Fixed-bias voltage differs from the declared voltage or supply limits.")
        else:
            raise ValueError("Expected exactly one calibrated-current or fixed-voltage bias control.")
        power = condition.supply_v * supply_current
        if power <= 0:
            raise ValueError("Nonpositive supply power; check Vdd current orientation.")
        result.bias = bias
        result.output_v = output_v
        result.bias_voltage_v = bias_voltage
        result.tail_current_a = tail_current
        result.dc_power_w = power
        nodes = circuit_nodes(circuit)
        node_rows = read_table(run_dir / "nodes.tsv", len(nodes) + 1)
        if len(node_rows) != 1:
            raise ValueError("Expected exactly one node-voltage operating point.")
        result.nodes = dict(zip(nodes, node_rows[0][1:]))
        result.metrics = parse_ac(run_dir / "ac.tsv", power) if analysis == "ac" else None
        if analysis == "tran":
            result.transient = parse_transient(run_dir / "transient.tsv", condition.supply_v)
        result.status = "ok"
    except subprocess.TimeoutExpired:
        result.status = "timeout"
        result.error = (
            f"ngspice exceeded {timeout_s:g} s and was killed. Inspect "
            f"{run_dir / 'ngspice.log'}; check convergence or increase the timeout."
        )
    except (OSError, ValueError, RuntimeError) as error:
        result.error = f"{error} (run: {run_dir})"
    return result


def main() -> int:
    candidate = Candidate(
        W_in=10, L_in=1, W_load=5, L_load=1, W_tail=5, L_tail=1, I_bias=25
    )
    print("Model: PTM 180 nm (BSIM3); no fallback")
    first = simulate(candidate)
    if first.status != "ok" or first.metrics is None:
        print(f"SIM FAIL: {first.error}", file=sys.stderr)
        return 1
    print(
        f"25 uA: gain={first.metrics.gain_db:.2f} dB, "
        f"UGF={first.metrics.ugf_hz / 1e6:.2f} MHz, "
        f"PM={first.metrics.pm_deg:.2f} deg, "
        f"power={first.metrics.power_w * 1e6:.2f} uW"
    )
    for name, point in first.bias.items():
        print(
            f"{name}: vds={point.vds:.4f} V, vdsat={point.vdsat:.4f} V, "
            f"vgs={point.vgs:.4f} V, |id|={point.id_a * 1e6:.2f} uA"
        )
    if first.warnings:
        print(
            f"Model warnings: {len(first.warnings)} (ignored model parameters; "
            f"see {Path(first.run_dir) / 'ngspice.log'})"
        )
    second = simulate(candidate.model_copy(update={"I_bias": 50}))
    if second.status != "ok" or second.metrics is None:
        print(f"SIM FAIL: doubled-bias simulation: {second.error}", file=sys.stderr)
        return 1
    ratio = second.metrics.power_w / first.metrics.power_w
    print(f"50 uA: power={second.metrics.power_w * 1e6:.2f} uW, ratio={ratio:.3f}")
    broken = simulate(candidate, netlist="Broken netlist\nRbad out 0 nonsense\n.end\n")
    print(f"Broken netlist: {broken.status}: {broken.error}")
    if (
        not 1.9 <= ratio <= 2.1
        or not math.isclose(first.metrics.power_w, 45e-6, rel_tol=0.01)
        or not 20 <= first.metrics.gain_db <= 60
        or not 1e6 <= first.metrics.ugf_hz <= 200e6
        or any(p.vds < p.vdsat or p.vgs <= p.vth for p in first.bias.values())
        or broken.status != "error"
        or not 0 < first.metrics.pm_deg < 180
    ):
        print("SIM FAIL: gain, UGF, power, bias, phase or error-path check failed.",
              file=sys.stderr)
        return 1
    print("SIM PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
