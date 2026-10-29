"""Bounded structured model calls shared by diagnostic and intake modes."""

from collections.abc import Callable
import json
import time
from typing import TypeVar

from pydantic import BaseModel

from agent.planner import ChatFunction, ConfigError, LLMConfig, LLMError, chat_completion
from agent.tracer import TraceEvent, Tracer, json_text


T = TypeVar("T", bound=BaseModel)


class Reasoner:
    def __init__(
        self, tracer: Tracer, chat: ChatFunction | None = None,
    ) -> None:
        self.tracer, self.calls = tracer, 0
        if chat is None:
            config = LLMConfig.from_env()
            self.chat = lambda messages: chat_completion(config, messages)
            self.model = config.model
        else:
            self.chat, self.model = chat, "injected-test-model"

    def ask(
        self, schema: type[T], instruction: str, facts: dict, *,
        validate: Callable[[T], object] | None = None,
    ) -> T:
        context = json_text(facts)
        if len(context) > 18000:
            raise ValueError("Reasoning context exceeds the compact-facts limit.")
        system = instruction + "\nReturn JSON only, matching this schema:\n" + json.dumps(
            schema.model_json_schema())
        error_text = ""
        for attempt in (1, 2):
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": context + (
                    "\nPrevious response rejected: " + error_text if error_text else "")},
            ]
            started = time.perf_counter()
            self.calls += 1
            outputs: dict = {"valid": False}
            try:
                response = self.chat(messages)
                outputs["usage"] = response.usage
                reply = schema.model_validate_json(response.content)
                if validate is not None:
                    validate(reply)
                outputs.update(valid=True, reply=reply.model_dump(mode="json"))
                return reply
            except ConfigError as error:
                outputs["error"] = str(error)
                raise
            except (LLMError, ValueError) as error:
                error_text = str(error)[:800]
                outputs["error"] = error_text
            finally:
                self.tracer.emit(TraceEvent(
                    type="model_call", duration_ms=(time.perf_counter() - started) * 1000,
                    inputs={"model": self.model, "attempt": attempt,
                            "schema": schema.__name__, "facts_chars": len(context),
                            "context_chars": sum(len(message["content"]) for message in messages),
                            "messages": len(messages)},
                    outputs=outputs,
                ))
        raise LLMError(f"{schema.__name__} failed both validated model attempts: {error_text}")
