"""LLM access. One method: send a prompt, get back a validated pydantic object.

Output that fails the schema (or the caller's semantic check) is retried once with
the error fed back; a second failure raises, and callers write nothing.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Callable, Protocol, TypeVar

import pydantic

from .config import Settings

log = logging.getLogger(__name__)

T = TypeVar("T", bound=pydantic.BaseModel)
Validator = Callable[[pydantic.BaseModel], None]

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(Exception):
    """The model answered, but not with something we can use."""


class LLMUnavailable(LLMError):
    """No model configured, or the API could not be reached. Safe to retry later."""


class LLM(Protocol):
    name: str

    def structured(
        self,
        *,
        task: str,
        system: str,
        user: str,
        schema: type[T],
        effort: str,
        validate: Callable[[T], None] | None = None,
    ) -> T: ...


def _strip_fences(text: str) -> str:
    text = text.strip()
    match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    return match.group(1) if match else text


class ClaudeLLM:
    name = "claude"

    def __init__(self, settings: Settings):
        import anthropic

        self._anthropic = anthropic
        self.settings = settings
        self.client = anthropic.Anthropic(max_retries=2, timeout=180.0)
        # Some accounts or models may reject structured outputs for a large schema;
        # remember that and fall back to schema-in-prompt plus local validation.
        self._structured_ok = True

    def structured(self, *, task, system, user, schema, effort, validate=None):
        a = self._anthropic
        messages: list[dict] = [{"role": "user", "content": user}]
        last_error = "unknown"
        for attempt in range(2):
            try:
                text, stop = self._call(system, messages, schema, effort)
            except a.BadRequestError as exc:
                if self._structured_ok and "schema" in str(exc).lower():
                    log.warning("Structured output rejected for %s; using prompt-only JSON: %s", task, exc)
                    self._structured_ok = False
                    text, stop = self._call(system, messages, schema, effort)
                else:
                    raise LLMError(f"{task}: request rejected: {exc}") from exc
            except (a.APIConnectionError, a.RateLimitError, a.InternalServerError) as exc:
                raise LLMUnavailable(f"{task}: Claude API unavailable: {exc}") from exc
            except a.AuthenticationError as exc:
                raise LLMUnavailable(f"{task}: API key rejected") from exc
            except a.APIStatusError as exc:
                raise LLMUnavailable(f"{task}: API error {exc.status_code}") from exc

            if stop == "refusal":
                raise LLMError(f"{task}: the model declined this request")
            try:
                if stop == "max_tokens":
                    raise ValueError("output was cut off (max_tokens)")
                result = schema.model_validate_json(_strip_fences(text))
                if validate:
                    validate(result)
                return result
            except (pydantic.ValidationError, ValueError) as exc:
                last_error = str(exc)[:2000]
                log.warning("%s: invalid output on attempt %d: %s", task, attempt + 1, last_error)
                # A fresh single-turn request (not a replayed assistant turn) keeps the
                # retry independent of the first attempt's thinking.
                messages = [
                    {
                        "role": "user",
                        "content": user
                        + "\n\n<previous_attempt>\n"
                        + (text or "(empty)")[:20000]
                        + "\n</previous_attempt>\nThat attempt was rejected: "
                        + last_error
                        + "\nReturn the complete corrected JSON object.",
                    }
                ]
        raise LLMError(f"{task}: output failed validation twice: {last_error}")

    def _call(self, system: str, messages: list[dict], schema, effort: str) -> tuple[str, str]:
        system_blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        output_config: dict = {"effort": effort}
        if self._structured_ok:
            from anthropic.lib._parse._transform import transform_schema

            output_config["format"] = {"type": "json_schema", "schema": transform_schema(schema)}
        else:
            system_blocks.append(
                {
                    "type": "text",
                    "text": "Respond with one JSON object matching this JSON schema, and nothing else:\n"
                    + json.dumps(schema.model_json_schema()),
                }
            )
        with self.client.beta.messages.stream(
            model=self.settings.model,
            max_tokens=32000,
            system=system_blocks,
            messages=messages,
            output_config=output_config,
            betas=[FALLBACK_BETA],
            fallbacks="default",
        ) as stream:
            response = stream.get_final_message()
        text = "".join(b.text for b in response.content if b.type == "text")
        return text, response.stop_reason or ""


class OfflineLLM:
    """Used when no model is configured. Capture still works; extraction waits."""

    name = "off"

    def structured(self, *, task, **_):
        raise LLMUnavailable(f"{task}: no LLM configured (set BAROMETER_LLM=claude and ANTHROPIC_API_KEY)")


def make_llm(settings: Settings) -> LLM:
    if settings.llm == "claude":
        return ClaudeLLM(settings)
    if settings.llm == "fake":
        from .llm_fake import FakeLLM

        return FakeLLM()
    return OfflineLLM()
