"""Shared, budgeted operating-point probes for the supported OTA circuit family."""

import argparse
from collections.abc import Callable
import json
from pathlib import Path
import re
import time
from uuid import uuid4

from pydantic import BaseModel
from typing import TypeVar

from agent.memory import Memory, CachedSimulator
from agent.planner import ChatFunction
from agent.reasoning import Reasoner
from agent.sim import Analysis, Candidate, Condition, SimulationResult, render_netlist
from agent.smoke import ROOT
from agent.tracer import TraceEvent, Tracer, json_text


DEFAULT_CANDIDATE = Candidate(
    W_in=10, L_in=1, W_load=5, L_load=1, W_tail=5, L_tail=1, I_bias=25,
)
T = TypeVar("T", bound=BaseModel)


def circuit_body(path: Path | None = None) -> str:
    text = (path or ROOT / "circuits" / "ota5t.cir.j2").read_text(encoding="utf-8")
    body = text.split(".control", 1)[0].rstrip()
    body = re.sub(r"(?im)^\s*\.end\s*$", "", body)
    devices = [line.split()[0].lower() for line in body.splitlines()
               if re.match(r"(?i)^\s*m\S+\s", line)]
    if sorted(devices) != ["m1", "m2", "m3", "m4", "m5"]:
        raise ValueError("Diagnostics currently support only the M1-M5 OTA adapter.")
    return body


def devices(body: str) -> dict[str, list[str]]:
    return {words[0].lower(): words[1:6] for line in body.splitlines()
            if (words := line.lower().split()) and words[0] in ("m1", "m2", "m3", "m4", "m5")}


def components(body: str, candidate: Candidate, condition: Condition) -> list[dict]:
    rendered = render_netlist(candidate, condition, "op", body).split(".control", 1)[0]
    return [{"name": words[0], "nodes": words[1:3], "definition": " ".join(words[3:])}
            for line in rendered.splitlines()[1:]
            if (words := line.split()) and words[0][0].lower() in "rclvid"
            and len(words) >= 4]


class ProbeSession:
    def __init__(
        self, mode: str, directory: Path, candidate: Candidate, budget: int, *,
        chat: ChatFunction | None = None, step_limit: int = 4, clipping: bool = False,
    ) -> None:
        self.memory = Memory(directory / "memory.sqlite3")
        self.tracer = Tracer(mode, "probes", directory)
        self.simulator = CachedSimulator(self.memory, self.tracer)
        self.candidate, self.budget, self.mode = candidate, budget, mode
        self.probes: list[dict] = []
        self.hypotheses: list[dict] = []
        self.chat, self.step_limit, self.clipping = chat, step_limit, clipping
        self.reasoner: Reasoner | None = None
        self.started = time.perf_counter()
        self.tracer.emit(TraceEvent(type="task_start", inputs={
            "mode": mode, "budget": budget, "candidate": candidate.model_dump(),
        }))

    def remaining(self) -> int:
        return self.budget - self.memory.task_counts(self.mode, "probes")[0]

    def ask(
        self, schema: type[T], instruction: str, facts: dict, *,
        validate: Callable[[T], object] | None = None,
    ) -> T:
        if self.reasoner is None:
            self.reasoner = Reasoner(self.tracer, self.chat)
        return self.reasoner.ask(schema, instruction, facts, validate=validate)

    def compact_probes(self) -> list[dict]:
        selected = self.probes[:1] + self.probes[-3:] if len(self.probes) > 4 else self.probes
        return [{"name": probe["name"], "candidate": probe["candidate"],
                 "condition": probe["result"]["condition"],
                 "bias": probe["result"]["bias"], "nodes": probe["result"]["nodes"],
                 "dc_power_w": probe["result"]["dc_power_w"],
                 "bias_voltage_v": probe["result"]["bias_voltage_v"],
                 "tail_current_a": probe["result"]["tail_current_a"],
                 "fixed_bias_v": probe["fixed_bias_v"],
                 "transient": probe["result"]["transient"]}
                for probe in selected]

    def probe(
        self, name: str, body: str, *, candidate: Candidate | None = None,
        condition: Condition | None = None, analysis: Analysis = "op",
        fixed_bias_v: float | None = None,
    ) -> SimulationResult:
        actual = candidate or self.candidate
        actual_condition = condition or Condition()
        netlist = (render_netlist(actual, actual_condition, analysis, body,
                                  fixed_bias_v=fixed_bias_v)
                   if fixed_bias_v is not None else None)
        answer = self.simulator.simulate(
            actual, actual_condition, analysis=analysis, circuit=body, netlist=netlist,
            before_run=lambda: self.memory.reserve_simulation(
                self.mode, "probes", name, self.budget, 1),
        )
        self.memory.save_job(self.mode, "probes", name, answer.result,
                             cache_hit=answer.cache_hit, done=True)
        self.probes.append({"name": name, "fingerprint": answer.fingerprint,
                            "candidate": actual.model_dump(mode="json"),
                            "circuit_body": body, "fixed_bias_v": fixed_bias_v,
                            "result": answer.result.model_dump(mode="json")})
        if answer.result.status != "ok":
            raise RuntimeError(f"Probe {name} failed: {answer.result.error}")
        return answer.result


def run_diagnostic(
    mode: str, *, directory: Path, netlist: Path | None = None,
    candidate: Candidate = DEFAULT_CANDIDATE, budget: int = 6, planted_fault: bool = False,
    chat: ChatFunction | None = None, step_limit: int = 4,
    clipping_fault: bool = False,
) -> dict:
    if mode not in ("debug", "explain") or budget < 1 or not 1 <= step_limit <= 8:
        raise ValueError("Choose debug/explain, a positive budget and a step cap between one and eight.")
    directory.mkdir(parents=True, exist_ok=False)
    session = ProbeSession(mode, directory, candidate, budget, chat=chat,
                           step_limit=step_limit, clipping=clipping_fault)
    result: dict = {}
    try:
        body = circuit_body(netlist)
        if planted_fault:
            body, count = re.subn(r"(?im)^(\s*M4\s+\S+\s+)\S+", r"\g<1>vdd", body)
            if count != 1:
                raise ValueError("Cannot plant fault without exactly one M4.")
        if clipping_fault:
            if mode != "debug" or planted_fault or re.search(r"(?im)^\s*Rload\b", body):
                raise ValueError("Clipping fault requires debug, no other fault and no existing Rload.")
            body += "\nRload out 0 100\n"
        if mode == "explain":
            from agent.explain import explain
            result = explain(body, session)
        else:
            from agent.debug_mode import diagnose
            result = diagnose(body, session)
    except (OSError, ValueError, RuntimeError) as error:
        result = {"status": "error", "verified_success": False, "error": str(error),
                  "review_text": f"Diagnostic failed: {error}"}
    used, hits = session.memory.task_counts(mode, "probes")
    elapsed = time.perf_counter() - session.started
    result.update(mode=mode, simulations_used=used, cache_hits=hits, budget=budget,
                  wall_clock_s=elapsed, trace=str(session.tracer.path), probes=session.probes,
                  engineer_verdict="not_requested_diagnostic",
                  model_calls=session.reasoner.calls if session.reasoner else 0,
                  step_limit=step_limit, hypotheses=session.hypotheses, clipping_fault=clipping_fault,
                  scope="Fixed OTA adapter; baseline gate bias is calibrated to the supplied I_bias. "
                        "Debug counterfactuals freeze that voltage unless I_bias itself is the intervention; "
                        "transient checks freeze the corresponding measured DC bias. "
                        "Model reasoning is bounded; electrical verdicts and quoted numbers are code-owned. "
                        "This is not arbitrary-topology diagnosis.")
    session.tracer.tool("write_proposed_lesson", session.memory.store_lesson, mode,
        f"{mode} ended {result['status']}; {len(session.probes)} measured DC probes. "
        "Diagnostic observations are unvalidated notes, not reusable sizing priors.")
    session.tracer.emit(TraceEvent(type="task_end", duration_ms=elapsed * 1000, outputs={
        "outcome": result["status"], "simulations_used": used, "cache_hits": hits,
        "wall_clock_s": elapsed, "engineer_verdict": result["engineer_verdict"],
    }))
    (directory / "result.json").write_text(json_text(result) + "\n", encoding="utf-8")
    (directory / "report.md").write_text(result["review_text"] + "\n\n" + result["scope"],
                                         encoding="utf-8")
    return result


def main(mode: str, argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"Simulator-grounded OTA {mode}.")
    parser.add_argument("--netlist", type=Path)
    parser.add_argument("--candidate", type=Path, help="JSON with dimensions and I_bias in uA.")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--budget", type=int, default=6)
    parser.add_argument("--steps", type=int, default=4, help="Maximum model-selected experiments (1-8).")
    if mode == "debug":
        parser.add_argument("--planted-fault", action="store_true")
        parser.add_argument("--clipping-fault", action="store_true",
                            help="Plant an excessive resistive load and measure transient clipping.")
    args = parser.parse_args(argv)
    try:
        candidate = Candidate.model_validate_json(args.candidate.read_text()) if args.candidate else DEFAULT_CANDIDATE
        result = run_diagnostic(
            mode, directory=args.out or ROOT / "runs" / mode / uuid4().hex,
            netlist=args.netlist, candidate=candidate, budget=args.budget,
            planted_fault=getattr(args, "planted_fault", False),
            step_limit=args.steps,
            clipping_fault=getattr(args, "clipping_fault", False),
        )
    except (OSError, ValueError) as error:
        print(f"{mode.upper()} FAIL: {error}")
        return 2
    print(result["review_text"])
    print(f"Scope: {result['scope']}")
    print(f"Simulations: {result['simulations_used']}; cache hits: {result['cache_hits']}")
    print(f"Trace: {result['trace']}")
    return 0 if result["verified_success"] else 1
