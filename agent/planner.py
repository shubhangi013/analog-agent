"""Planner: one bounded JSON request per round to an Azure AI Foundry deployment.

Configuration comes from environment variables (or mini-agent/.env):
  LLM_PROVIDER=azure_foundry
  LLM_MODEL=<deployment name>
  AZURE_FOUNDRY_ENDPOINT=https://<resource>.services.ai.azure.com
  AZURE_FOUNDRY_API_KEY=<key>
Optional: LLM_TIMEOUT_S (default 60), LLM_JSON_MODE (default 1).
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from email.message import Message as HTTPMessage
from http.client import HTTPResponse
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Literal, NoReturn
import urllib.error
from urllib.parse import urlsplit
import urllib.request

from pydantic import (
    BaseModel, ConfigDict, Field, JsonValue, SecretStr, ValidationError, model_validator,
)

from agent.context import PlannerState, build_context
from agent.sim import Candidate
from agent.smoke import ROOT
from agent.tools import candidate_key, make_candidates
from agent.tracer import TraceEvent, Tracer


CANDIDATES_PER_ROUND = 6
FALLBACK_SPREAD = 0.5
PLACEHOLDER = re.compile(r"[<>]|placeholder|your[-_]?(resource|key|deployment)", re.IGNORECASE)
HTTP_HINTS = {
    400: "check deployment support for JSON mode; try LLM_JSON_MODE=0",
    401: "check AZURE_FOUNDRY_API_KEY",
    403: "the key or network rules do not allow this resource",
    404: "check AZURE_FOUNDRY_ENDPOINT and that LLM_MODEL is a deployment name",
    429: "rate limited; wait or raise the deployment's quota",
}


class ConfigError(RuntimeError):
    """The planner is not configured; message says which variable to fix."""


class LLMError(RuntimeError):
    """The model call failed (network, HTTP, or unusable response)."""


def read_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE reader for mini-agent/.env; real environment variables win."""
    if not path.is_file():
        return {}
    values = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key.strip()):
            raise ConfigError(f"Invalid .env assignment at line {number}; use KEY=VALUE.")
        value = value.strip()
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise ConfigError(f"Unclosed .env quote at line {number}.")
            value = value[1:-1]
        values[key.strip()] = value
    return values


class LLMConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    provider: Literal["azure_foundry"]
    model: str
    endpoint: str
    api_key: SecretStr
    timeout_s: float = Field(default=60, gt=0, le=600)
    json_mode: bool = True

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "LLMConfig":
        if env is None:
            env = {**read_env_file(ROOT / ".env"), **os.environ}
        provider = env.get("LLM_PROVIDER", "azure_foundry").strip().lower()
        if provider != "azure_foundry":
            raise ConfigError(f"LLM_PROVIDER={provider!r} is not supported; use azure_foundry.")
        names = ("LLM_MODEL", "AZURE_FOUNDRY_ENDPOINT", "AZURE_FOUNDRY_API_KEY")
        missing = [
            name for name in names
            if not env.get(name, "").strip() or PLACEHOLDER.search(env[name])
        ]
        if missing:
            raise ConfigError(
                f"Set {', '.join(missing)} (missing or still a placeholder). "
                "See README 'Configure Azure AI Foundry'."
            )
        endpoint = env["AZURE_FOUNDRY_ENDPOINT"].strip()
        try:
            parts = urlsplit(endpoint)
            parts.port
        except ValueError:
            raise ConfigError("AZURE_FOUNDRY_ENDPOINT is not a valid resource URL.") from None
        local = parts.hostname in ("localhost", "127.0.0.1")
        if parts.scheme != "https" and not (local and parts.scheme == "http"):
            raise ConfigError("AZURE_FOUNDRY_ENDPOINT must start with https://.")
        if (
            not parts.hostname or parts.query or parts.fragment
            or parts.username is not None or parts.password is not None
            or parts.path.rstrip("/") not in ("", "/openai/v1")
        ):
            raise ConfigError("AZURE_FOUNDRY_ENDPOINT must be a plain resource URL.")
        try:
            timeout_s = float(env.get("LLM_TIMEOUT_S", "60"))
            json_mode = {"1": True, "true": True, "0": False, "false": False}[
                env.get("LLM_JSON_MODE", "1").strip().lower()
            ]
            return cls(
                provider="azure_foundry", model=env["LLM_MODEL"].strip(), endpoint=endpoint,
                api_key=SecretStr(env["AZURE_FOUNDRY_API_KEY"].strip()),
                timeout_s=timeout_s, json_mode=json_mode,
            )
        except (KeyError, ValueError) as error:
            raise ConfigError(f"Invalid LLM_TIMEOUT_S or LLM_JSON_MODE: {error}") from None

    @property
    def url(self) -> str:
        """Foundry v1 OpenAI-compatible route; no api-version query is needed."""
        base = self.endpoint.rstrip("/")
        base = re.sub(r"/openai/v1$", "", base)
        return f"{base}/openai/v1/chat/completions"


@dataclass(frozen=True)
class ChatResponse:
    content: str
    usage: dict[str, int] = field(default_factory=dict)


Message = dict[str, str]
ChatFunction = Callable[[list[Message]], ChatResponse]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: urllib.request.Request, fp: HTTPResponse, code: int, msg: str,
        headers: HTTPMessage, newurl: str,
    ) -> NoReturn:
        raise LLMError("Foundry redirected the request; configure the final resource endpoint.")


def chat_completion(config: LLMConfig, messages: list[Message]) -> ChatResponse:
    body: dict[str, JsonValue] = {"model": config.model, "messages": messages}
    if config.json_mode:
        body["response_format"] = {"type": "json_object"}
    request = urllib.request.Request(
        config.url, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json",
                 "api-key": config.api_key.get_secret_value()},
    )
    try:
        opener = urllib.request.build_opener(NoRedirect())
        with opener.open(request, timeout=config.timeout_s) as response:
            raw = response.read(65537)
            if len(raw) > 65536:
                raise LLMError("Foundry response exceeds the 64 KiB planner response limit.")
            payload = json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read(4096).decode("utf-8", "replace").replace(
            config.api_key.get_secret_value(), "[REDACTED]"
        )[:300]
        hint = HTTP_HINTS.get(error.code, "see response detail")
        if error.code in (400, 401, 403, 404):
            raise ConfigError(f"Foundry HTTP {error.code} ({hint}): {detail}") from None
        raise LLMError(f"Foundry HTTP {error.code} ({hint}): {detail}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise LLMError(f"Could not reach {config.url}: {error}") from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise LLMError("Foundry response body was not JSON.") from None
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise LLMError("Response has no choices[0].message.content "
                       "(content filter or unsupported response shape).") from None
    if not isinstance(content, str) or not content.strip():
        raise LLMError("Model returned empty content.")
    usage = payload.get("usage")
    if usage is not None and not isinstance(usage, dict):
        raise LLMError("Foundry usage must be an object when present.")
    tokens = {
        key: value for key, value in (usage or {}).items()
        if isinstance(value, int) and not isinstance(value, bool)
    }
    return ChatResponse(content, tokens)


class PlannerReply(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)
    hypothesis: str = Field(min_length=1, max_length=600)
    candidates: list[Candidate]
    stop: bool

    @model_validator(mode="after")
    def six_distinct(self) -> "PlannerReply":
        prose = re.sub(r"\bM[1-5]\b", "", self.hypothesis, flags=re.I)
        if re.search(r"\d", prose):
            raise ValueError("Keep hypotheses qualitative; numeric settings belong in candidates "
                             "and measured results come only from tools.")
        if len(self.candidates) != CANDIDATES_PER_ROUND:
            raise ValueError(
                f"expected exactly {CANDIDATES_PER_ROUND} candidates, got {len(self.candidates)}"
            )
        if len({candidate_key(item) for item in self.candidates}) != CANDIDATES_PER_ROUND:
            raise ValueError("the candidates must all be different")
        return self


def parse_reply(text: str, tried: set[tuple[float, ...]]) -> PlannerReply:
    """Raise ValueError with a short message the model can act on."""
    fenced = re.fullmatch(r"\s*```(?:json)?\s*(.*?)\s*```\s*", text, re.DOTALL)
    try:
        data = json.loads(fenced.group(1) if fenced else text)
    except json.JSONDecodeError as error:
        raise ValueError(f"reply is not valid JSON ({error.msg} at line {error.lineno})") from None
    try:
        reply = PlannerReply.model_validate(data)
    except ValidationError as error:
        problems = [
            f"{'.'.join(str(part) for part in issue['loc']) or 'reply'}: {issue['msg']}"
            for issue in error.errors()[:8]
        ]
        raise ValueError("; ".join(problems)) from None
    repeated = [i for i, item in enumerate(reply.candidates) if candidate_key(item) in tried]
    if repeated:
        raise ValueError(f"candidates at positions {repeated} repeat candidates already tried")
    return reply


@dataclass(frozen=True)
class Plan:
    reply: PlannerReply
    source: Literal["model", "model_retry", "fallback"]
    errors: list[str]


class Planner:
    def __init__(self, chat: ChatFunction, tracer: Tracer, model_name: str = "test") -> None:
        self.chat = chat
        self.tracer = tracer
        self.model_name = model_name

    @classmethod
    def from_env(cls, tracer: Tracer) -> "Planner":
        config = LLMConfig.from_env()
        return cls(lambda messages: chat_completion(config, messages), tracer, config.model)

    def propose(
        self, state: PlannerState, *, base: Candidate,
        tried: Iterable[Candidate], round_index: int,
    ) -> Plan:
        tried_list = list(tried)
        tried_keys = {candidate_key(item) for item in tried_list}
        errors: list[str] = []
        retry_error: str | None = None
        for attempt in (1, 2):
            context = self.tracer.tool("build_context", build_context, state)
            messages: list[Message] = [
                {"role": "system", "content": context.system()},
                {"role": "user", "content": context.user()},
            ]
            if retry_error is not None:
                messages.append({"role": "user", "content": (
                    f"Your reply was invalid: {retry_error}. Reply again with JSON only: "
                    f"exactly {CANDIDATES_PER_ROUND} new, distinct candidates inside "
                    "the RANGES. Set stop=false unless all-condition success is confirmed."
                )})
            start = time.perf_counter()
            content: str | None = None
            usage: dict[str, int] = {}
            try:
                response = self.chat(messages)
                content, usage = response.content, response.usage
                reply = parse_reply(content, tried_keys)
                if reply.stop and not state.all_conditions_passed:
                    raise ValueError("stop=true requires verifier-confirmed all-condition success")
            except ConfigError as error:
                self._trace_call(round_index, attempt, messages, start, usage, error=str(error))
                raise
            except (LLMError, ValueError) as error:
                errors.append(f"attempt {attempt}: {error}")
                self._trace_call(round_index, attempt, messages, start, usage,
                                 error=str(error), excerpt=content)
                if isinstance(error, ValueError) and content is not None:
                    retry_error = str(error)[:800]
                continue
            self._trace_call(round_index, attempt, messages, start, usage, reply=reply)
            return Plan(reply, "model" if attempt == 1 else "model_retry", errors)

        start = time.perf_counter()
        candidates = make_candidates(
            base, CANDIDATES_PER_ROUND, FALLBACK_SPREAD, seed=round_index, exclude=tried_list,
        )
        reply = PlannerReply(
            hypothesis=(
                "Fallback: the planner reply was unusable twice; varying every value "
                "over a broad bounded region around the best so far."
            ),
            candidates=candidates, stop=False,
        )
        self.tracer.emit(TraceEvent(
            type="tool_call", duration_ms=(time.perf_counter() - start) * 1000,
            inputs={"tool": "make_candidates", "round": round_index,
                    "base": base.model_dump(mode="json"),
                    "n": CANDIDATES_PER_ROUND, "spread": FALLBACK_SPREAD},
            outputs={"hypothesis": reply.hypothesis, "candidates": len(candidates),
                     "reason": errors[-1][:300] if errors else None},
        ))
        return Plan(reply, "fallback", errors)

    def _trace_call(
        self, round_index: int, attempt: int, messages: list[Message], start: float,
        usage: dict[str, int], *, reply: PlannerReply | None = None,
        error: str | None = None, excerpt: str | None = None,
    ) -> None:
        outputs: dict[str, JsonValue] = {"valid": reply is not None, "usage": dict(usage)}
        if reply is not None:
            outputs.update(hypothesis=reply.hypothesis,
                           candidates=len(reply.candidates), stop=reply.stop)
        else:
            outputs.update(error=(error or "")[:300], reply_excerpt=(excerpt or "")[:300])
        self.tracer.emit(TraceEvent(
            type="model_call", duration_ms=(time.perf_counter() - start) * 1000,
            inputs={"round": round_index, "attempt": attempt, "model": self.model_name,
                    "messages": len(messages),
                    "context_chars": sum(len(item["content"]) for item in messages)},
            outputs=outputs,
        ))


def main() -> int:
    from uuid import uuid4

    from agent.context import demo_state
    from agent.memory import CachedSimulator, Memory

    try:
        config = LLMConfig.from_env()
    except ConfigError as error:
        print(f"PLANNER NOT CONFIGURED: {error}", file=sys.stderr)
        return 2
    print(f"Provider: azure_foundry, deployment: {config.model}, url: {config.url}")
    tracer = Tracer("planner_demo", uuid4().hex)
    state = demo_state(CachedSimulator(Memory(), tracer))
    context = build_context(state)
    print(f"Context: {len(context.system())} system + {len(context.user())} user characters")
    planner = Planner(lambda messages: chat_completion(config, messages), tracer, config.model)
    assert state.best is not None
    try:
        plan = planner.propose(
            state, base=state.best.candidate,
            tried=[row.candidate for row in state.latest], round_index=1,
        )
    except ConfigError as error:
        print(f"PLANNER NOT CONFIGURED: {error}", file=sys.stderr)
        return 2
    print(f"Source: {plan.source}")
    for error in plan.errors:
        print(f"Rejected: {error}")
    print(f"Hypothesis: {plan.reply.hypothesis}")
    print("W_in L_in W_load L_load W_tail L_tail I_bias")
    for candidate in plan.reply.candidates:
        print(" ".join(f"{value:g}" for value in candidate.model_dump().values()))
    print(f"stop: {plan.reply.stop}")
    print(f"Trace: {tracer.path}")
    if plan.source == "fallback":
        print("PLANNER FAIL: the model never returned a valid reply; fallback used.",
              file=sys.stderr)
        return 1
    print("PLANNER PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
