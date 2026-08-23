"""OpenAI adapter.

Uses Chat Completions with a strict ``json_schema`` response format, so the
proposal contract is enforced at the API boundary as well as revalidated
locally. Strict mode requires every property to be listed in ``required`` and
``additionalProperties: false`` -- which the proposal schema already satisfies,
because those are the same constraints that stop the model smuggling an amount
or a message body past it.

The SDK imports lazily, so nothing here is needed unless a live run is asked
for.
"""

from __future__ import annotations

import json
import time
from typing import Any

from recovery.agent.client import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_TOKENS,
    DEFAULT_TIMEOUT_SECONDS,
    ModelReply,
)

DEFAULT_MODEL = "gpt-5-mini"

# Micro-dollars per token, by model. Populated only where we are confident of
# list pricing; anything absent reports zero cost and is read in tokens
# instead. Under a free daily allowance the marginal price is zero anyway --
# tokens are the constraint that actually binds, so they are what the report
# leads with.
PRICING_MICROS: dict[str, tuple[int, int]] = {}


class OpenAIClient:
    """Calls an OpenAI chat model with a strict JSON schema."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._max_tokens = max_tokens
        self._max_retries = max_retries
        self._client: Any | None = None

    def _ensure_client(self) -> Any:
        if self._client is None:
            import openai

            self._client = openai.OpenAI(
                timeout=self._timeout_seconds, max_retries=self._max_retries
            )
        return self._client

    def propose(self, *, system: str, prompt: str, schema: dict[str, Any]) -> ModelReply:
        started = time.monotonic()
        try:
            import openai
        except ImportError:
            return ModelReply.failure(
                "openai SDK not installed; install the 'openai' extra", model=self._model
            )

        try:
            response = self._ensure_client().chat.completions.create(
                model=self._model,
                # max_completion_tokens, not max_tokens: the reasoning models
                # reject the older parameter outright.
                max_completion_tokens=self._max_tokens,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "recovery_action_proposal",
                        "strict": True,
                        "schema": schema,
                    },
                },
            )
        except openai.APIStatusError as exc:
            return self._failed(f"api_status_{exc.status_code}", started)
        except openai.APIConnectionError:
            return self._failed("api_connection_error", started)
        except openai.OpenAIError as exc:
            return self._failed(f"sdk_error:{type(exc).__name__}", started)

        return self._read(response, started)

    def _read(self, response: Any, started: float) -> ModelReply:
        """Turn a completion into a reply, without trusting its shape."""
        latency_ms = int((time.monotonic() - started) * 1000)
        usage = getattr(response, "usage", None)
        inputs = int(getattr(usage, "prompt_tokens", 0) or 0)
        outputs = int(getattr(usage, "completion_tokens", 0) or 0)

        def reply(payload: dict[str, Any] | None, error: str | None) -> ModelReply:
            return ModelReply(
                payload=payload,
                ok=error is None,
                error=error,
                model=self._model,
                input_tokens=inputs,
                output_tokens=outputs,
                cost_micros=self._cost_micros(inputs, outputs),
                latency_ms=latency_ms,
            )

        choices = getattr(response, "choices", None) or []
        if not choices:
            return reply(None, "no_choices")

        choice = choices[0]
        # A length stop means the JSON is truncated and unparseable; say so
        # rather than reporting it as malformed output.
        if getattr(choice, "finish_reason", None) == "length":
            return reply(None, "max_tokens_reached")

        message = getattr(choice, "message", None)
        if getattr(message, "refusal", None):
            return reply(None, "refusal")

        content = getattr(message, "content", None)
        if not content:
            return reply(None, "empty_content")

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return reply(None, "invalid_json")
        if not isinstance(parsed, dict):
            return reply(None, "not_an_object")
        return reply(parsed, None)

    def _cost_micros(self, inputs: int, outputs: int) -> int:
        rates = PRICING_MICROS.get(self._model)
        if rates is None:
            return 0
        return inputs * rates[0] + outputs * rates[1]

    def _failed(self, reason: str, started: float) -> ModelReply:
        return ModelReply.failure(
            reason, model=self._model, latency_ms=int((time.monotonic() - started) * 1000)
        )
