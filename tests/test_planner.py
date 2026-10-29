from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread
from typing import Any, Iterator

import pytest

from agent.context import PlannerState, build_context
from agent.planner import (
    ChatResponse, ConfigError, LLMConfig, LLMError, Planner, chat_completion,
    parse_reply, read_env_file,
)
from agent.sim import Candidate
from agent.tools import candidate_key, make_candidates
from agent.tracer import Tracer
from agent.verifier import Targets


BASE = Candidate(W_in=10, L_in=1, W_load=5, L_load=1, W_tail=5, L_tail=1, I_bias=25)
SIX = make_candidates(BASE, 6, 0.6, seed=42)
KEY = "test-secret-key-123"
ENV = {
    "LLM_PROVIDER": "azure_foundry", "LLM_MODEL": "gpt-test",
    "AZURE_FOUNDRY_ENDPOINT": "https://example.services.ai.azure.com",
    "AZURE_FOUNDRY_API_KEY": KEY,
}


def reply_json(candidates: list[Candidate] = SIX, **overrides: Any) -> str:
    data: dict[str, Any] = {
        "hypothesis": "bandwidth has margin; trade some current for gain",
        "candidates": [item.model_dump() for item in candidates], "stop": False,
    }
    data.update(overrides)
    return json.dumps(data)


class Scripted:
    def __init__(self, *replies: str | Exception) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict[str, str]]] = []

    def __call__(self, messages: list[dict[str, str]]) -> ChatResponse:
        self.calls.append([dict(item) for item in messages])
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return ChatResponse(item, {"total_tokens": 100})


def propose(tmp_path: Path, chat: Scripted, tried: list[Candidate] = [BASE]):
    tracer = Tracer("planner", "test", tmp_path)
    state = PlannerState(targets=Targets(
        gain_db=30, ugf_hz=20e6, pm_deg=60, power_w=200e-6))
    plan = Planner(chat, tracer, "gpt-test").propose(
        state, base=BASE, tried=tried, round_index=3)
    events = [json.loads(line) for line in tracer.path.read_text().splitlines()]
    return plan, events


def test_valid_reply_first_try(tmp_path: Path) -> None:
    chat = Scripted(reply_json())
    plan, events = propose(tmp_path, chat)
    assert plan.source == "model" and plan.errors == []
    assert plan.reply.candidates == SIX
    assert [m["role"] for m in chat.calls[0]] == ["system", "user"]
    assert chat.calls[0][0]["content"].startswith("You are the planner")
    assert [e["type"] for e in events] == ["tool_call", "model_call"]
    assert events[0]["inputs"]["tool"] == "build_context"
    assert events[1]["outputs"]["hypothesis"].startswith("bandwidth has margin")
    assert events[1]["inputs"]["round"] == 3


@pytest.mark.parametrize("bad,expected", [
    ("not json", "not valid JSON"),
    (reply_json(SIX[:5]), "exactly 6 candidates"),
    (reply_json([SIX[0]] * 6), "all be different"),
    (reply_json([BASE, *SIX[:5]]), "repeat candidates already tried"),
    (reply_json(hypothesis=""), "hypothesis"),
    (reply_json(hypothesis="gain 35"), "qualitative"),
    (reply_json(hypothesis="PM 90 degrees"), "qualitative"),
    (reply_json(extra="x"), "extra"),
    (json.dumps({"hypothesis": "h", "stop": False,
                 "candidates": [{**SIX[0].model_dump(), "I_bias": 500}] + [
                     c.model_dump() for c in SIX[1:]]}), "candidates.0.I_bias"),
])
def test_invalid_then_valid_retry_shows_error(tmp_path: Path, bad: str, expected: str) -> None:
    chat = Scripted(bad, reply_json())
    plan, events = propose(tmp_path, chat)
    assert plan.source == "model_retry"
    assert expected in plan.errors[0]
    retry = chat.calls[1]
    assert len(retry) == 3
    assert retry[2]["role"] == "user" and expected in retry[2]["content"]
    assert [e["outputs"]["valid"] for e in events if e["type"] == "model_call"] == [False, True]


def test_two_invalid_replies_fall_back(tmp_path: Path) -> None:
    chat = Scripted("nope", reply_json(SIX[:2]))
    plan, events = propose(tmp_path, chat)
    assert plan.source == "fallback" and len(plan.errors) == 2
    assert len(plan.reply.candidates) == 6 and not plan.reply.stop
    keys = {candidate_key(item) for item in plan.reply.candidates}
    assert len(keys) == 6 and candidate_key(BASE) not in keys
    assert plan.reply.hypothesis.startswith("Fallback")
    assert [e["type"] for e in events] == [
        "tool_call", "model_call", "tool_call", "model_call", "tool_call",
    ]
    assert events[-1]["inputs"]["tool"] == "make_candidates"


def test_transport_errors_fall_back_without_echo(tmp_path: Path) -> None:
    chat = Scripted(LLMError("HTTP 500"), LLMError("HTTP 500"))
    plan, events = propose(tmp_path, chat)
    assert plan.source == "fallback"
    assert len(chat.calls[1]) == 2


def test_stop_still_requires_six_candidates() -> None:
    with pytest.raises(ValueError, match="exactly 6"):
        parse_reply(json.dumps({"hypothesis": "done", "candidates": [], "stop": True}), set())


def test_unconfirmed_stop_is_retried(tmp_path: Path) -> None:
    plan, _ = propose(tmp_path, Scripted(reply_json(stop=True), reply_json()))
    assert plan.source == "model_retry" and not plan.reply.stop
    assert "verifier-confirmed" in plan.errors[0]


def test_confirmed_stop_keeps_six_candidates(tmp_path: Path) -> None:
    state = PlannerState(
        targets=Targets(gain_db=30, ugf_hz=20e6, pm_deg=60, power_w=200e-6),
        all_conditions_passed=True,
    )
    chat = Scripted(reply_json(stop=True))
    plan = Planner(chat, Tracer("stop", "test", tmp_path)).propose(
        state, base=BASE, tried=[BASE], round_index=1,
    )
    assert plan.source == "model" and plan.reply.stop
    assert len(plan.reply.candidates) == 6


def test_retry_rebuilds_bounded_context_from_current_state(tmp_path: Path) -> None:
    state = PlannerState(targets=Targets(
        gain_db=30, ugf_hz=20e6, pm_deg=60, power_w=200e-6))
    calls = []

    def chat(messages):
        calls.append(messages)
        state.simulations_used = 5
        return ChatResponse("bad" * 10000 if len(calls) == 1 else reply_json())

    plan = Planner(chat, Tracer("retry", "test", tmp_path)).propose(
        state, base=BASE, tried=[BASE], round_index=1,
    )
    assert plan.source == "model_retry"
    assert "0 of 40" in calls[0][1]["content"]
    assert "5 of 40" in calls[1][1]["content"]
    assert all(len(message["content"]) < 9000 for message in calls[1])
    assert not any(message["role"] == "assistant" for message in calls[1])


@pytest.mark.parametrize("stop", ["false", 0])
def test_stop_must_be_boolean(stop) -> None:
    with pytest.raises(ValueError, match="stop"):
        parse_reply(reply_json(stop=stop), set())


def test_fenced_json_is_accepted() -> None:
    assert parse_reply(f"```json\n{reply_json()}\n```", set()).candidates == SIX


def test_from_env_reads_foundry_settings() -> None:
    config = LLMConfig.from_env(ENV)
    assert config.model == "gpt-test"
    assert config.url == "https://example.services.ai.azure.com/openai/v1/chat/completions"
    assert KEY not in repr(config) and KEY not in str(config.model_dump())
    trailing = LLMConfig.from_env({**ENV, "AZURE_FOUNDRY_ENDPOINT":
                                   "https://example.openai.azure.com/openai/v1/"})
    assert trailing.url == "https://example.openai.azure.com/openai/v1/chat/completions"


@pytest.mark.parametrize("change,message", [
    ({"LLM_MODEL": ""}, "LLM_MODEL"),
    ({"AZURE_FOUNDRY_API_KEY": "<your-api-key>"}, "AZURE_FOUNDRY_API_KEY"),
    ({"AZURE_FOUNDRY_ENDPOINT": "placeholder"}, "AZURE_FOUNDRY_ENDPOINT"),
    ({"AZURE_FOUNDRY_ENDPOINT": "http://example.com"}, "https"),
    ({"AZURE_FOUNDRY_ENDPOINT": "https://x.com?api-version=1"}, "plain"),
    ({"AZURE_FOUNDRY_ENDPOINT": "https://x.com/api/projects/demo"}, "plain"),
    ({"AZURE_FOUNDRY_ENDPOINT": "https://name:password@x.com"}, "plain"),
    ({"AZURE_FOUNDRY_ENDPOINT": "https://x.com:bad"}, "valid resource URL"),
    ({"LLM_PROVIDER": "openai"}, "not supported"),
    ({"LLM_TIMEOUT_S": "abc"}, "LLM_TIMEOUT_S"),
    ({"LLM_JSON_MODE": "maybe"}, "LLM_JSON_MODE"),
])
def test_config_errors_are_actionable(change: dict[str, str], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        LLMConfig.from_env({**ENV, **change})


class FakeFoundry(BaseHTTPRequestHandler):
    requests: list[tuple[str, dict[str, str], dict[str, Any]]] = []
    status = 200
    payload: Any = {}
    headers_to_send: dict[str, str] = {}

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append(
            (self.path, {k.lower(): v for k, v in self.headers.items()}, body))
        data = json.dumps(type(self).payload).encode()
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for key, value in type(self).headers_to_send.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def server() -> Iterator[LLMConfig]:
    FakeFoundry.requests, FakeFoundry.status = [], 200
    FakeFoundry.headers_to_send = {}
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeFoundry)
    Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield LLMConfig.from_env({**ENV, "AZURE_FOUNDRY_ENDPOINT":
                                  f"http://127.0.0.1:{httpd.server_port}", "LLM_TIMEOUT_S": "5"})
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_http_request_shape(server: LLMConfig) -> None:
    FakeFoundry.payload = {"choices": [{"message": {"content": reply_json()}}],
                           "usage": {"prompt_tokens": 7, "completion_tokens": 3}}
    response = chat_completion(server, [{"role": "user", "content": "hi"}])
    assert parse_reply(response.content, set()).candidates == SIX
    assert response.usage == {"prompt_tokens": 7, "completion_tokens": 3}
    path, headers, body = FakeFoundry.requests[0]
    assert path == "/openai/v1/chat/completions"
    assert headers["api-key"] == KEY and "authorization" not in headers
    assert body == {"model": "gpt-test", "messages": [{"role": "user", "content": "hi"}],
                    "response_format": {"type": "json_object"}}


def test_json_mode_can_be_disabled(server: LLMConfig) -> None:
    FakeFoundry.payload = {"choices": [{"message": {"content": "{}"}}]}
    chat_completion(server.model_copy(update={"json_mode": False}), [])
    assert "response_format" not in FakeFoundry.requests[0][2]


@pytest.mark.parametrize("status,payload,message", [
    (200, {"choices": []}, "no choices"),
    (200, {"choices": [{"message": {"content": ""}}]}, "empty content"),
    (200, {"choices": [{"message": {"content": None}}]}, "empty content"),
])
def test_http_failures_raise_llm_error(
    server: LLMConfig, status: int, payload: Any, message: str,
) -> None:
    FakeFoundry.status, FakeFoundry.payload = status, payload
    with pytest.raises(LLMError, match=message.replace("(", r"\(").replace(")", r"\)")):
        chat_completion(server, [])


def test_unreachable_endpoint_is_llm_error() -> None:
    config = LLMConfig.from_env({**ENV, "AZURE_FOUNDRY_ENDPOINT": "http://127.0.0.1:9",
                                 "LLM_TIMEOUT_S": "2"})
    with pytest.raises(LLMError, match="Could not reach"):
        chat_completion(config, [])


def test_end_to_end_over_http_never_logs_key(server: LLMConfig, tmp_path: Path) -> None:
    FakeFoundry.payload = {"choices": [{"message": {"content": reply_json()}}]}
    tracer = Tracer("planner", "http", tmp_path)
    planner = Planner(lambda messages: chat_completion(server, messages), tracer, server.model)
    state = PlannerState(targets=Targets(
        gain_db=30, ugf_hz=20e6, pm_deg=60, power_w=200e-6))
    context = build_context(state)
    plan = planner.propose(state, base=BASE, tried=[BASE], round_index=1)
    assert plan.source == "model"
    sent = FakeFoundry.requests[0][2]["messages"]
    assert sent[0]["content"] == context.system() and sent[1]["content"] == context.user()
    assert KEY not in tracer.path.read_text()
    print("\nSource: model (local mock Foundry, no cloud call)")
    print(plan.reply.model_dump_json(indent=2))


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_configuration_http_errors_never_fall_back(
    server: LLMConfig, tmp_path: Path, status: int,
) -> None:
    FakeFoundry.status = status
    FakeFoundry.payload = {"error": {"message": f"bad setting (echo {KEY})"}}
    tracer = Tracer("config", "error", tmp_path)
    state = PlannerState(targets=Targets(
        gain_db=30, ugf_hz=20e6, pm_deg=60, power_w=200e-6))
    planner = Planner(lambda messages: chat_completion(server, messages), tracer)
    with pytest.raises(ConfigError, match=f"HTTP {status}") as error:
        planner.propose(state, base=BASE, tried=[BASE], round_index=1)
    assert KEY not in str(error.value)
    assert KEY not in tracer.path.read_text()
    events = [json.loads(line) for line in tracer.path.read_text().splitlines()]
    assert len(events) == 2 and events[1]["outputs"]["valid"] is False
    assert len(FakeFoundry.requests) == 1


@pytest.mark.parametrize("usage", ["bad shape", [1, 2]])
def test_malformed_usage_is_actionable(server: LLMConfig, usage) -> None:
    FakeFoundry.payload = {
        "choices": [{"message": {"content": reply_json()}}], "usage": usage,
    }
    with pytest.raises(LLMError, match="usage must be an object"):
        chat_completion(server, [])


def test_env_file_values_and_environment_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent import planner

    monkeypatch.setattr(planner, "ROOT", tmp_path)
    path = tmp_path / ".env"
    path.write_text("\n".join(f"{key}='{value}'" for key, value in ENV.items()), encoding="utf-8")
    assert read_env_file(path) == ENV
    for key in (*ENV, "LLM_TIMEOUT_S", "LLM_JSON_MODE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LLM_MODEL", "environment-deployment")
    assert LLMConfig.from_env().model == "environment-deployment"


@pytest.mark.parametrize("line", ["malformed", "INVALID NAME=value", "LLM_MODEL='unclosed"])
def test_bad_env_file_is_not_silently_ignored(tmp_path: Path, line: str) -> None:
    path = tmp_path / ".env"
    path.write_text(line, encoding="utf-8")
    with pytest.raises(ConfigError, match="line 1"):
        read_env_file(path)


def test_redirect_is_rejected_without_forwarding_key(server: LLMConfig) -> None:
    FakeFoundry.status = 302
    FakeFoundry.payload = {}
    FakeFoundry.headers_to_send = {"Location": server.endpoint + "/redirect-target"}
    with pytest.raises(LLMError, match="redirected"):
        chat_completion(server, [])
    assert len(FakeFoundry.requests) == 1


def test_response_size_is_bounded(server: LLMConfig) -> None:
    FakeFoundry.payload = {"oversized": "x" * 70000}
    with pytest.raises(LLMError, match="64 KiB"):
        chat_completion(server, [])
