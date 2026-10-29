import json
from pathlib import Path

import pytest

from agent.intake import (
    ClarificationNeeded, ConstraintBinding, IntakeReply, bound_request, interpret_request,
)
from agent.planner import ChatResponse
from agent.reasoning import Reasoner
from agent.tracer import Tracer
from agent.verifier import Targets


DEFAULTS = Targets(gain_db=30, ugf_hz=20e6, pm_deg=60, power_w=0.0002)


def proposal(quote: str, target: str = "ugf_hz") -> IntakeReply:
    return IntakeReply(objective="feasibility",
                       constraints=[ConstraintBinding(target=target, quote=quote)])


def test_free_text_targets_are_extracted_from_source_not_generated_by_the_model(tmp_path: Path) -> None:
    goal = "I need at least 35 dB of amplification and at least 25 megahertz of bandwidth, power at most 150 microwatts"
    reply = IntakeReply(objective="feasibility", constraints=[
        ConstraintBinding(target="gain_db", quote="I need at least 35 dB of amplification"),
        ConstraintBinding(target="ugf_hz", quote="at least 25 megahertz of bandwidth"),
        ConstraintBinding(target="power_w", quote="power at most 150 microwatts"),
    ])
    reasoner = Reasoner(Tracer("intake", "test", tmp_path),
                        lambda messages: ChatResponse(reply.model_dump_json()))
    request = interpret_request(goal, DEFAULTS, reasoner)
    assert request.targets == Targets(gain_db=35, ugf_hz=25e6, pm_deg=60, power_w=0.00015)
    assert request.goal == goal and reasoner.calls == 1
    assert any("pm_deg" in assumption for assumption in request.assumptions)


@pytest.mark.parametrize("goal,quote,target", [
    ("bandwidth at least 25 megahertz", "bandwidth at least 30 megahertz", "ugf_hz"),
    ("bandwidth at least 25 megahertz", "25 megahertz", "ugf_hz"),
    ("gain at least 35 dB", "gain at least 35 dB", "power_w"),
])
def test_intake_rejects_invented_values_truncated_qualifiers_and_unit_mismatches(
    goal: str, quote: str, target: str,
) -> None:
    with pytest.raises(ValueError):
        bound_request(goal, DEFAULTS, proposal(quote, target))


@pytest.mark.parametrize("goal,target", [
    ("bandwidth at most 25 megahertz", "ugf_hz"),
    ("power at least 150 microwatts", "power_w"),
    ("gain about 35 dB", "gain_db"),
    ("gain > 35 dB", "gain_db"),
])
def test_ambiguous_or_reversed_comparisons_require_engineer_clarification(goal: str, target: str) -> None:
    with pytest.raises(ClarificationNeeded):
        bound_request(goal, DEFAULTS, proposal(goal, target))


def test_unsupported_remaining_quantity_cannot_be_silently_discarded() -> None:
    with pytest.raises(ClarificationNeeded):
        bound_request("bandwidth at least 25 MHz and supply 1.8 V", DEFAULTS,
                      proposal("bandwidth at least 25 MHz"))


def test_ambiguous_model_reply_asks_a_question_without_a_second_model_call(tmp_path: Path) -> None:
    reasoner = Reasoner(Tracer("intake", "question", tmp_path), lambda messages: ChatResponse(
        json.dumps({"objective": "feasibility", "constraints": [],
                    "questions": ["What minimum bandwidth, and in which units?"]})))
    with pytest.raises(ClarificationNeeded, match="minimum bandwidth"):
        interpret_request("Make the bandwidth fast", DEFAULTS, reasoner)
    assert reasoner.calls == 1
