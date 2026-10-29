"""Dependency-free reports from persisted evaluation results; no model-generated scores."""

import argparse
from collections import defaultdict
from html import escape
import json
from pathlib import Path
import statistics
import sys


def progress_svg(results: list[dict]) -> str:
    width, height = 900, 420
    left, top, plot_w, plot_h = 80, 40, 590, 310
    series = []
    for result in results:
        points = [
            (point["simulations_used"], point["best_complete_worst_margin"])
            for point in result.get("progress", [])
            if point["best_complete_worst_margin"] is not None
        ]
        if points:
            series.append((result, points))
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        '<g font-family="sans-serif" font-size="12" fill="#222">',
        '<text x="80" y="22">Best complete five-condition target margin (higher is better)</text>',
    ]
    if not series:
        svg.append('<text x="80" y="100">No candidate has complete simulator metrics at all five conditions.</text>')
    else:
        max_x = max(1, max(x for _, points in series for x, _ in points))
        low = min(-0.1, min(y for _, points in series for _, y in points))
        high = max(0.1, max(y for _, points in series for _, y in points))
        colors = ("#0072b2", "#d55e00", "#009e73", "#cc79a7", "#e69f00", "#333333")
        for index in range(5):
            value = low + (high - low) * index / 4
            y = top + plot_h * (high - value) / (high - low)
            svg.append(
                f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" stroke="#ddd"/>'
                f'<text x="8" y="{y + 4:.1f}">{value:.1%}</text>'
            )
        for index, (result, points) in enumerate(series):
            color = colors[index % len(colors)]
            coordinates = " ".join(
                f"{left + plot_w * x / max_x:.1f},{top + plot_h * (high - y) / (high - low):.1f}"
                for x, y in points
            )
            label = escape(f"{result['case_id']} {result['strategy']} s{result['seed']}")
            svg.append(
                f'<polyline points="{coordinates}" fill="none" stroke="{color}" stroke-width="2">'
                f'<title>{label}</title></polyline>'
            )
            for x, y in points:
                svg.append(
                    f'<circle cx="{left + plot_w * x / max_x:.1f}" '
                    f'cy="{top + plot_h * (high - y) / (high - low):.1f}" r="2" fill="{color}"/>'
                )
            svg.append(f'<text x="690" y="{50 + index * 16}" fill="{color}">{label}</text>')
        svg.append(
            f'<text x="{left}" y="375">0</text>'
            f'<text x="{left + plot_w - 15}" y="375">{max_x}</text>'
            '<text x="230" y="402">Actual ngspice executions (cache hits are free)</text>'
        )
    return "\n".join([*svg, "</g></svg>"]) + "\n"


def write_report(directory: Path) -> Path:
    results = json.loads((directory / "results.json").read_text(encoding="utf-8"))
    lines = [
        "# Circuit-engineer evaluation", "",
        "Numbers come from stored ngspice results and the deterministic verifier. "
        "Each case/strategy/seed starts with an empty database. The random baseline uses "
        "uniform legal candidates, including its initial population; both strategies use the "
        "same production scheduler, conditions, budget and four-worker limit.", "",
        "**Scope:** feasibility cases stop after a successful round; objective cases select "
        "the best observed verified design, not a proven global optimum. Cases specify explicit targets. "
        "Approval is automatic for evaluation, not a human verdict. Export round trips are skipped "
        "to avoid unbudgeted simulations. Random seeds do not seed the remote model; the agent "
        "is measured once per case. No statistical superiority is implied by one agent run.", "",
        "| Case | Strategy / seed | Outcome | Verified pass | Hard checks | Judge /4 | Simulations | First pass at | Cache hits | Wall s |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for result in results:
        lines.append(
            f"| {result['case_id']} | {result['strategy']} / {result['seed']} | "
            f"{result['status']} | {'yes' if result['verified_success'] else 'no'} | "
            f"{result.get('heuristic_status', 'not run')} | "
            f"{result.get('llm_judge', {}).get('mean_score', '-')} | "
            f"{result.get('simulations_used', '-')} | "
            f"{result.get('simulations_to_first_pass') if result.get('simulations_to_first_pass') is not None else '-'} | "
            f"{result.get('cache_hits', '-')} | {result.get('wall_clock_s', 0):.3f} |"
        )
    lines.extend([
        "", "## Two independent assessment layers", "",
        "**Hard checks** revalidate candidates, all-condition success, metric provenance, simulation "
        "budgets, cache accounting and trace lifecycle. A task may fail to find a design while still "
        "passing these integrity checks.", "",
        "**LLM judge** scores goal alignment, numerical grounding, trade-off reasoning, experimental "
        "reasoning and limitation honesty from 0 to 4, with required evidence ids. Its opinion "
        "cannot override electrical verification or a failed hard check. Judge inputs, validated "
        "outputs, timings and token usage are persisted separately. It sees bounded structured "
        "evidence, not raw simulator output. Unless --judge-model selects another deployment, "
        "the judge uses the planner deployment, so shared-model bias remains a limitation.", "",
    ])
    for result in results:
        judge = result.get("llm_judge", {})
        if judge.get("status") == "scored":
            lines.append(f"### {result['case_id']} / {result['strategy']} / {result['seed']}")
            lines.append("")
            for name, judgment in judge["dimensions"].items():
                rationale = judgment["rationale"].replace("\n", " ")
                lines.append(
                    f"- {name}: {judgment['score']}/4. {rationale} "
                    f"(evidence: {', '.join(judgment['evidence'])})"
                )
            lines.append("")
        elif judge.get("status") == "error":
            lines.append(f"- {result['case_id']} / {result['strategy']}: judge failed; no score assigned.")
        for check in result.get("heuristic_checks", []):
            if check["status"] == "fail":
                lines.append(f"- FAILED {check['name']}: {check['evidence']}")
    lines.extend(["", "## Baseline comparison", ""])
    grouped = defaultdict(list)
    for result in results:
        if result["mode"] == "optimize":
            grouped[result["case_id"]].append(result)
    for case_id, runs in grouped.items():
        random_runs = [run for run in runs if run["strategy"] == "random"]
        if random_runs:
            passes = [run["simulations_to_first_pass"] for run in random_runs
                      if run["verified_success"] and run["simulations_to_first_pass"] is not None]
            median = f"{statistics.median(passes):g}" if passes else "not reached"
            lines.append(
                f"- {case_id}: random verified success {len(passes)}/{len(random_runs)}; "
                f"median simulations to first pass among successes: {median}. "
                "Failures are censored, not zero-cost successes."
            )
    lines.extend([
        "", "## Best result versus simulations used", "",
        "![Simulator-backed progress](progress.svg)", "",
        "Each point uses only candidates whose five condition results had completed by that event. "
        "An incomplete or simulator-error candidate cannot improve this curve. A nonnegative "
        "metric margin alone is not a pass: transistor bias and all hard checks must also pass.", "",
        "## Simulated trade-off archive", "",
        "These are evaluation-derived nondominated feasible alternatives; actual interactive "
        "review text is also retained in each result. Numbers are conservative "
        "worst-case values across all five conditions. Full per-condition metrics, bias checks "
        "and raw-output locations are retained in each result.json.", "",
        "| Case / strategy / seed | Candidate | Gain dB | UGF MHz | PM deg | Power uW |",
        "| --- | --- | --- | --- | --- | --- |",
    ])
    option_count = 0
    for result in results:
        for option in result.get("pareto_options", []):
            metrics = option["worst_case_metrics"]
            lines.append(
                f"| {result['case_id']} / {result['strategy']} / {result['seed']} | "
                f"{option['candidate_id']} | {metrics['gain_db']:.3f} | "
                f"{metrics['ugf_hz'] / 1e6:.3f} | {metrics['pm_deg']:.3f} | "
                f"{metrics['power_w'] * 1e6:.3f} |"
            )
            option_count += 1
    if not option_count:
        lines.append("| No fully verified feasible alternatives observed | - | - | - | - | - |")
    lines.extend(["", "## Missing capabilities and limitations", ""])
    for result in results:
        if result["status"] == "missing":
            lines.append(f"- {result['case_id']}: {result['reason']} Expected: {'; '.join(result['expected'])}")
        if result.get("error"):
            lines.append(f"- {result['case_id']} / {result['strategy']}: {result['error']}")
        if result.get("fallback_calls"):
            lines.append(
                f"- {result['case_id']} / {result['strategy']}: {result['fallback_calls']} planner "
                "fallback rounds; do not attribute those candidates to the model."
            )
        if result.get("expected_feasible") is False:
            lines.append(
                f"- {result['case_id']} is deliberately infeasible; budget exhaustion is not a "
                "proof that the agent recognized or explained infeasibility."
            )
        if not result.get("objective_supported", True):
            lines.append(
                f"- {result['case_id']}: requested power minimization is unsupported; "
                "a verified feasible result does not satisfy this optimization objective."
            )
    lines.extend([
        "- Lesson ablation in this benchmark: **not measured**. Retrieval is disabled to isolate "
        "agent versus random search. Run eval.learning for the separate sequential, held-out-guarded experiment.",
        "- Debug/explain use bounded model reasoning and budgeted simulator probes for the fixed OTA "
        "adapter. The clipping scenario checks a resistive overload, not arbitrary transient faults.",
        "- queue_wait_s includes worker-pool waiting and cache-claim contention; simulation_run_s "
        "and end-to-end wall_clock_s are recorded separately.",
        "- This suite evaluates the fixed five-transistor OTA and PTM model, not arbitrary topologies or process kits.",
        "- Run failures and missing modes remain visible; CLI success means the benchmark completed, "
        "not that every design task or capability passed.",
        "", "Raw evidence: manifest.json, results.json and each case's result.json, SQLite database "
        "and JSONL trace. Source/model/suite hashes and simulator version are in the manifest.", "",
    ])
    (directory / "progress.svg").write_text(progress_svg(results), encoding="utf-8")
    path = directory / "report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args(argv)
    try:
        print(write_report(args.directory))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"REPORT ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
