"""Export a stored design as a standalone sized netlist, report and JSON, then re-simulate it."""

import argparse
from collections.abc import Callable
import json
import re
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any
from uuid import uuid4

from agent.memory import CachedSimulator, Memory, StoredDesign
from agent.sim import Condition, SimulationResult, ngspice_version, render_netlist, simulate
from agent.smoke import ROOT, find_ngspice
from agent.tracer import TraceEvent, Tracer
from agent.verifier import verify

ROUND_TRIP_RTOL = 1e-3
ROUND_TRIP_ATOL = 1e-6
METRIC_FIELDS = ("gain_db", "ugf_hz", "pm_deg", "power_w")
TYPICAL = (27, 1.8)
Simulator = Callable[..., SimulationResult]


def export_design(
    design_id: int, out_dir: Path, *, memory: Memory | None = None,
    simulator: Simulator = simulate, executable: str | None = None,
) -> dict[str, Any]:
    memory = memory if memory is not None else Memory()
    design = memory.design(design_id)
    executable = executable if executable is not None else find_ngspice()
    body = design.circuit_body or None
    typical = next(
        result.condition for result in design.results
        if (result.condition.temperature_c, result.condition.supply_v) == TYPICAL
    )
    names = ["sized" if result.condition == typical else result.condition.name
             for result in design.results]
    if len(set(names)) != len(names) or any(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,100}", name) is None for name in names
    ):
        raise ValueError("Export condition filenames must be safe and unique.")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "sized.cir").write_text(
        render_netlist(design.candidate, typical, "ac", body), encoding="utf-8",
    )
    shutil.copyfile(ROOT / "models" / "ptm180.lib", out_dir / "ptm180.lib")
    corners = []
    for stored in design.results:
        path = out_dir / ("sized.cir" if stored.condition == typical else
                          f"{stored.condition.name}.cir")
        if path.name != "sized.cir":
            path.write_text(render_netlist(design.candidate, stored.condition, "ac", body),
                            encoding="utf-8")
        corners.append(_round_trip_corner(
            design, stored.condition, stored, path.read_text(encoding="utf-8"),
            (out_dir / "ptm180.lib").read_bytes(), simulator, executable,
        ))
    summary: dict[str, Any] = {
        "design_id": design.id,
        "circuit": "engineer netlist" if body else "built-in OTA",
        "circuit_sha": design.circuit_sha,
        "provenance": design.provenance,
        "ngspice_version": ngspice_version(executable),
        "targets": design.targets.model_dump(),
        "candidate": design.candidate.model_dump(),
        "round_trip": {
            "passed": all(corner["passed"] for corner in corners),
            "tolerance": {"rel": ROUND_TRIP_RTOL, "abs": ROUND_TRIP_ATOL},
            "corners": corners,
        },
        "files": ["sized.cir", "ptm180.lib", "report.md", "design.json",
                  *[f"{result.condition.name}.cir" for result in design.results
                    if result.condition != typical]],
    }
    (out_dir / "design.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8",
    )
    (out_dir / "report.md").write_text(_report(design, summary), encoding="utf-8")
    return summary


def _round_trip_corner(
    design: StoredDesign, condition: Condition, stored: SimulationResult,
    text: str, model_contents: bytes, simulator: Simulator, executable: str,
) -> dict[str, Any]:
    rerun = simulator(
        design.candidate, condition, analysis="ac", netlist=text, executable=executable,
        model_contents=model_contents,
    )
    matches = (
        stored.metrics is not None and rerun.metrics is not None and all(
            math.isclose(
                getattr(stored.metrics, field), getattr(rerun.metrics, field),
                rel_tol=ROUND_TRIP_RTOL, abs_tol=ROUND_TRIP_ATOL,
            )
            for field in METRIC_FIELDS
        )
    )
    verified = rerun.metrics is not None and verify(
        design.candidate, rerun, design.targets,
    ).passed
    return {
        "condition": condition.name,
        "temperature_c": condition.temperature_c,
        "supply_v": condition.supply_v,
        "stored": stored.metrics.model_dump() if stored.metrics else None,
        "rerun": rerun.metrics.model_dump() if rerun.metrics else None,
        "matches": matches,
        "verified": verified,
        "passed": matches and verified,
        "error": rerun.error,
    }


def _version_line(version: str) -> str:
    return next(
        (line.strip("* ") for line in version.splitlines() if re.search(r"ngspice-\d", line)),
        "unknown",
    )


def _report(design: StoredDesign, summary: dict[str, Any]) -> str:
    candidate = design.candidate
    targets = design.targets
    round_trip = summary["round_trip"]
    lines = [
        f"# Sized design {design.id}",
        "",
        f"- Circuit: {summary['circuit']}"
        + (f" (sha {design.circuit_sha[:12]})" if design.circuit_sha else ""),
        f"- Task: {design.provenance.get('task_id', 'unknown')}, "
        f"run {design.provenance.get('run_id', 'unknown')}",
        f"- Simulations used: {design.provenance.get('simulations_used', 'unknown')}",
        f"- ngspice: {_version_line(summary['ngspice_version'])}",
        "",
        "## Sizing",
        "",
        "| Parameter | Value |",
        "|---|---|",
        f"| W_in | {candidate.W_in:.3f} um |",
        f"| L_in | {candidate.L_in:.3f} um |",
        f"| W_load | {candidate.W_load:.3f} um |",
        f"| L_load | {candidate.L_load:.3f} um |",
        f"| W_tail | {candidate.W_tail:.3f} um |",
        f"| L_tail | {candidate.L_tail:.3f} um |",
        f"| I_bias | {candidate.I_bias:.3f} uA |",
        "",
        "## Targets",
        "",
        f"- Gain >= {targets.gain_db:g} dB",
        f"- UGF >= {targets.ugf_hz / 1e6:g} MHz",
        f"- Phase margin >= {targets.pm_deg:g} deg",
        f"- Power <= {targets.power_w * 1e6:g} uW",
        "",
        "## Corners (stored results)",
        "",
        "| Corner | Gain (dB) | UGF (MHz) | PM (deg) | Power (uW) | Round trip |",
        "|---|---|---|---|---|---|",
    ]
    for corner in round_trip["corners"]:
        stored = corner["stored"]
        lines.append(
            f"| {corner['condition']} ({corner['temperature_c']:g} C, "
            f"{corner['supply_v']:g} V) | {stored['gain_db']:.2f} | "
            f"{stored['ugf_hz'] / 1e6:.2f} | {stored['pm_deg']:.2f} | "
            f"{stored['power_w'] * 1e6:.2f} | "
            f"{'PASS' if corner['passed'] else 'FAIL'} |"
        )
    lines += [
        "",
        "## Round trip",
        "",
        f"Revalidated every corner from exact exported netlist and model snapshots "
        f"(verified cache reuse allowed). Tolerance: "
        f"relative {ROUND_TRIP_RTOL:g}. Result: "
        f"**{'PASS' if round_trip['passed'] else 'FAIL'}**.",
        "",
        "## Reproduce",
        "",
        "Run `ngspice -b sized.cir` in this folder (typical corner). It writes `ac.tsv`. "
        "`ptm180.lib` must stay next to `sized.cir`.",
        "",
    ]
    return "\n".join(lines)


def export_task(
    design_id: int, out_dir: Path, *, memory: Memory, budget: int = 5,
) -> dict[str, Any]:
    if budget < 1:
        raise ValueError("Export simulation budget must be positive.")
    run_id = uuid4().hex
    tracer = Tracer("export", run_id, out_dir)
    simulator = CachedSimulator(memory, tracer)
    started = time.perf_counter()
    tracer.emit(TraceEvent(type="task_start", inputs={"design_id": design_id, "budget": budget}))
    outcome = "error"
    try:
        def replay(candidate, condition, **kwargs):
            job_id = f"export-{condition.name}"
            answer = simulator.simulate(
                candidate, condition, analysis="ac", netlist=kwargs["netlist"],
                model_contents=kwargs["model_contents"],
                before_run=lambda: memory.reserve_simulation("export", run_id, job_id, budget, 1),
            )
            memory.save_job("export", run_id, job_id, answer.result,
                            cache_hit=answer.cache_hit, done=True)
            return answer.result

        summary = export_design(design_id, out_dir, memory=memory, simulator=replay)
        used, hits = memory.task_counts("export", run_id)
        summary.update(simulations_used=used, cache_hits=hits, trace=str(tracer.path))
        (out_dir / "design.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        outcome = "pass" if summary["round_trip"]["passed"] else "delivery_failed"
        return summary
    finally:
        used, hits = memory.task_counts("export", run_id)
        tracer.emit(TraceEvent(type="task_end", outputs={
            "outcome": outcome, "simulations_used": used, "cache_hits": hits,
            "wall_clock_s": time.perf_counter() - started,
            "engineer_verdict": "not_requested_export_revalidation",
        }))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export a stored design as a sized netlist, report and JSON.",
    )
    parser.add_argument("--design", type=int, required=True, help="Design id from the library.")
    parser.add_argument("--budget", type=int, default=5)
    parser.add_argument("--memory", type=Path, default=ROOT / "runs" / "memory.sqlite3")
    parser.add_argument(
        "--out", type=Path, default=None,
        help="Output folder. Default: runs/exports/design_<id>.",
    )
    args = parser.parse_args(argv)
    out_dir = args.out if args.out is not None else ROOT / "runs" / "exports" / f"design_{args.design}"
    try:
        summary = export_task(args.design, out_dir, memory=Memory(args.memory), budget=args.budget)
    except (KeyError, OSError, RuntimeError, ValueError) as error:
        print(f"EXPORT FAIL: {error}", file=sys.stderr)
        return 2
    passed = summary["round_trip"]["passed"]
    print(f"Exported design {args.design} to {out_dir}")
    print(f"Round trip: {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
