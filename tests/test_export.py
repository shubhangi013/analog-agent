from pathlib import Path
from typing import Any
import json

import pytest

from agent import export
from agent.export import export_design, main
from agent.memory import Memory
from agent.sim import BiasPoint, Candidate, Condition, Metrics, SimulationResult
from agent.verifier import Targets


CANDIDATE = Candidate(W_in=10, L_in=1, W_load=5, L_load=1, W_tail=5, L_tail=1, I_bias=25)
TARGETS = Targets(gain_db=30, ugf_hz=20e6, pm_deg=60, power_w=200e-6)
METRICS = Metrics(gain_db=36, ugf_hz=25e6, pm_deg=75, power_w=45e-6)
CORNERS = (
    ("typical", 27, 1.8), ("hot_low", 125, 1.62), ("cold_low", -40, 1.62),
    ("hot_high", 125, 1.98), ("cold_high", -40, 1.98),
)
BODY = "* engineer body\n"


def corner_result(condition: Condition, metrics: Metrics = METRICS) -> SimulationResult:
    return SimulationResult(
        status="ok", condition=condition, analysis="ac", metrics=metrics,
        bias={f"m{i}": BiasPoint(vds=0.5, vdsat=0.2, vgs=0.6, vth=0.4, id_a=12.5e-6)
              for i in range(1, 6)},
        run_dir="stored",
    )


def stored_conditions() -> list[SimulationResult]:
    return [
        corner_result(Condition(name=name, temperature_c=temperature, supply_v=supply))
        for name, temperature, supply in CORNERS
    ]


@pytest.fixture
def memory(tmp_path: Path) -> Memory:
    return Memory(tmp_path / "memory.sqlite3")


@pytest.fixture
def stored(memory: Memory, monkeypatch: pytest.MonkeyPatch) -> tuple[Memory, int]:
    monkeypatch.setattr(export, "ngspice_version", lambda executable: "ngspice-47 test")
    design_id = memory.store_design(
        CANDIDATE, TARGETS, stored_conditions(), approved=True,
        circuit_sha="a" * 64, circuit_body=BODY,
        provenance={"task_id": "easy", "run_id": "run-1", "simulations_used": 11, "cache_hits": 0},
    )
    return memory, design_id


def replay(metrics: Metrics = METRICS, calls: list[dict[str, Any]] | None = None) -> Any:
    def simulator(candidate: Candidate, condition: Condition, **kwargs: Any) -> SimulationResult:
        if calls is not None:
            calls.append({"condition": condition.name, **kwargs})
        return corner_result(condition, metrics)
    return simulator


def test_export_writes_all_files_and_round_trip_passes(
    stored: tuple[Memory, int], tmp_path: Path,
) -> None:
    memory, design_id = stored
    out_dir = tmp_path / "out"
    summary = export_design(
        design_id, out_dir, memory=memory, simulator=replay(), executable="fake-ngspice",
    )
    for name in ("sized.cir", "ptm180.lib", "report.md", "design.json"):
        assert (out_dir / name).is_file(), name
    assert summary["round_trip"]["passed"] is True
    assert summary["circuit"] == "engineer netlist"
    assert summary["ngspice_version"] == "ngspice-47 test"
    on_disk = json.loads((out_dir / "design.json").read_text(encoding="utf-8"))
    assert on_disk["round_trip"]["passed"] is True
    assert on_disk["provenance"]["task_id"] == "easy"
    assert "Result: **PASS**" in (out_dir / "report.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ["..\\escape", "sized", "cold_low"])
def test_export_rejects_escaping_or_colliding_condition_filenames(
    stored: tuple[Memory, int], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str,
) -> None:
    memory, design_id = stored
    design = memory.design(design_id)
    results = list(design.results)
    results[1] = results[1].model_copy(update={
        "condition": results[1].condition.model_copy(update={"name": name}),
    })
    monkeypatch.setattr(memory, "design", lambda _: design.model_copy(update={"results": results}))
    out_dir = tmp_path / "out"
    with pytest.raises(ValueError, match="safe and unique"):
        export_design(design_id, out_dir, memory=memory, simulator=replay(),
                      executable="fake-ngspice")
    assert not out_dir.exists()


def test_sized_netlist_uses_engineer_body_and_keeps_measurements(
    stored: tuple[Memory, int], tmp_path: Path,
) -> None:
    memory, design_id = stored
    out_dir = tmp_path / "out"
    export_design(design_id, out_dir, memory=memory, simulator=replay(), executable="x")
    text = (out_dir / "sized.cir").read_text(encoding="utf-8")
    assert text.startswith(BODY)
    assert "wrdata ac.tsv v(out)" in text
    assert "{{" not in text


def test_every_corner_is_resimulated_from_netlist_text(
    stored: tuple[Memory, int], tmp_path: Path,
) -> None:
    memory, design_id = stored
    calls: list[dict[str, Any]] = []
    export_design(
        design_id, tmp_path / "out", memory=memory,
        simulator=replay(calls=calls), executable="x",
    )
    assert [call["condition"] for call in calls] == [name for name, _, _ in CORNERS]
    for call in calls:
        assert call["analysis"] == "ac"
        assert call["executable"] == "x"
        assert "wrdata ac.tsv v(out)" in call["netlist"]
        assert call["netlist"].startswith(BODY)


def test_round_trip_fails_when_rerun_metrics_drift(
    stored: tuple[Memory, int], tmp_path: Path,
) -> None:
    memory, design_id = stored
    drifted = Metrics(gain_db=METRICS.gain_db * 1.01, ugf_hz=METRICS.ugf_hz,
                      pm_deg=METRICS.pm_deg, power_w=METRICS.power_w)
    out_dir = tmp_path / "out"
    summary = export_design(
        design_id, out_dir, memory=memory, simulator=replay(drifted), executable="x",
    )
    assert summary["round_trip"]["passed"] is False
    assert all(corner["matches"] is False for corner in summary["round_trip"]["corners"])
    assert "Result: **FAIL**" in (out_dir / "report.md").read_text(encoding="utf-8")


def test_missing_design_raises_key_error(memory: Memory, tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        export_design(999, tmp_path / "out", memory=memory, simulator=replay(), executable="x")


def test_version_line_skips_the_banner() -> None:
    banner = "******\n** ngspice-47 : Circuit level simulation program\n******"
    assert export._version_line(banner) == "ngspice-47 : Circuit level simulation program"
    assert export._version_line("garbage") == "unknown"


def test_cli_exit_codes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def passing(design_id: int, out_dir: Path, **kwargs) -> dict[str, Any]:
        return {"round_trip": {"passed": True}}

    def failing(design_id: int, out_dir: Path, **kwargs) -> dict[str, Any]:
        return {"round_trip": {"passed": False}}

    def missing(design_id: int, out_dir: Path, **kwargs) -> dict[str, Any]:
        raise KeyError(design_id)

    monkeypatch.setattr(export, "export_design", passing)
    args = ["--design", "1", "--out", str(tmp_path), "--memory", str(tmp_path / "memory.sqlite3")]
    assert main(args) == 0
    monkeypatch.setattr(export, "export_design", failing)
    assert main(args) == 1
    monkeypatch.setattr(export, "export_design", missing)
    assert main(args) == 2
