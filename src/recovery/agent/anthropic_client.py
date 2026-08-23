"""Anthropic adapter.

Structured output is requested via ``output_config.format``. The reply is
revalidated by the caller regardless -- schema enforcement happens server-side
and this process cannot verify that it did.

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

DEFAULT_MODEL = "claude-opus-5"

# Micro-dollars per token: $5/1M input, $25/1M output. Integers, so accumulated
# cost never carries float error.
PRICING_MICROS: dict[str, tuple[int, int]] = {
    "claude-opus-5": (5, 25),
    "claude-sonnet-5": (3, 15),
    "claude-haiku-4-5": (1, 5),
}


class AnthropicClient:
    """Calls Claude with a JSON schema constraining the response."""

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
            import anthropic

            self._client = anthropic.Anthropic(
                timeout=self._timeout_seconds, max_retries=self._max_retries
            )
        return self._client

    def propose(self, *, system: str, prompt: str, schema: dict[str, Any]) -> ModelReply:
        started = time.monotonic()
        try:
            import anthropic
        except ImportError:
            return ModelReply.failure(
                "anthropic SDK not installed; install the 'anthropic' extra",
                model=self._model,
            )

        try:
            response = self._ensure_client().messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
        except anthropic.APIStatusError as exc:
            return self._failed(f"api_status_{exc.status_code}", started)
        except anthropic.APIConnectionError:
            return self._failed("api_connection_error", started)
        except anthropic.AnthropicError as exc:
            return self._failed(f"sdk_error:{type(exc).__name__}", started)

        return self._read(response, started)

    def _read(self, response: Any, started: float) -> ModelReply:
        latency_ms = int((time.monotonic() - started) * 1000)
        usage = getattr(response, "usage", None)
        inputs = int(getattr(usage, "input_tokens", 0) or 0)
        outputs = int(getattr(usage, "output_tokens", 0) or 0)

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

        if getattr(response, "stop_reason", None) == "refusal":
            return reply(None, "refusal")
        if getattr(response, "stop_reason", None) == "max_tokens":
            return reply(None, "max_tokens_reached")

        text = _first_text_block(response)
        if text is None:
            return reply(None, "no_text_block")
        try:
            parsed = json.loads(text)
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


def _first_text_block(response: Any) -> str | None:
    """Response content is a list of typed blocks; take the first text one."""
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text":
            return str(getattr(block, "text", ""))
    return None
