"""Model client boundary.

Three implementations behind one protocol:

* :class:`AnthropicClient` -- the real thing. Imports the SDK lazily so the
  policy engine, runner and metrics stay dependency-free and runnable offline.
* :class:`ScriptedClient` -- deterministic canned replies, for tests.
* :class:`UnavailableClient` -- always fails, to exercise the fallback path.

Every call carries a timeout, a token cap, a bounded retry, and a cost tag, and
every failure mode returns rather than raises. A model outage must degrade the
system to its rules path, not stop it.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

MODEL_ID = "claude-opus-5"

# Claude Opus 5 list pricing, in micro-dollars per token: $5/1M input,
# $25/1M output. Integers, so cost never accumulates float error.
INPUT_MICROS_PER_TOKEN = 5
OUTPUT_MICROS_PER_TOKEN = 25

DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_TOKENS = 1024
DEFAULT_MAX_RETRIES = 2


@dataclass(frozen=True, slots=True)
class ModelReply:
    """One model call: what came back, what it cost, and whether it worked."""

    payload: dict[str, Any] | None
    ok: bool
    error: str | None
    model: str
    cost_micros: int
    latency_ms: int

    @staticmethod
    def failure(error: str, *, model: str, latency_ms: int) -> ModelReply:
        return ModelReply(
            payload=None,
            ok=False,
            error=error,
            model=model,
            cost_micros=0,
            latency_ms=latency_ms,
        )


class LLMClient(Protocol):
    """What the agent planner needs from a model."""

    def propose(self, *, system: str, prompt: str, schema: dict[str, Any]) -> ModelReply: ...


class AnthropicClient:
    """Calls Claude with a JSON schema constraining the response.

    Structured output is requested via ``output_config.format``, and the reply
    is revalidated by the caller regardless -- schema enforcement happens on the
    server, and this process cannot verify it happened.
    """

    def __init__(
        self,
        *,
        model: str = MODEL_ID,
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
        """Import and construct the SDK client on first use."""
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
                "anthropic SDK not installed; install the 'agent' extra",
                model=self._model,
                latency_ms=0,
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
        except anthropic.AnthropicError as exc:  # anything else the SDK raises
            return self._failed(f"sdk_error:{type(exc).__name__}", started)

        latency_ms = int((time.monotonic() - started) * 1000)
        usage = getattr(response, "usage", None)
        cost = _cost_micros(usage)

        if getattr(response, "stop_reason", None) == "refusal":
            return ModelReply(
                payload=None,
                ok=False,
                error="refusal",
                model=self._model,
                cost_micros=cost,
                latency_ms=latency_ms,
            )

        text = _first_text_block(response)
        if text is None:
            return ModelReply(
                payload=None,
                ok=False,
                error="no_text_block",
                model=self._model,
                cost_micros=cost,
                latency_ms=latency_ms,
            )
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return ModelReply(
                payload=None,
                ok=False,
                error="invalid_json",
                model=self._model,
                cost_micros=cost,
                latency_ms=latency_ms,
            )
        return ModelReply(
            payload=payload,
            ok=True,
            error=None,
            model=self._model,
            cost_micros=cost,
            latency_ms=latency_ms,
        )

    def _failed(self, reason: str, started: float) -> ModelReply:
        return ModelReply.failure(
            reason, model=self._model, latency_ms=int((time.monotonic() - started) * 1000)
        )


def _cost_micros(usage: Any) -> int:
    """Micro-dollars for one call, from reported token usage."""
    if usage is None:
        return 0
    inputs = getattr(usage, "input_tokens", 0) or 0
    outputs = getattr(usage, "output_tokens", 0) or 0
    return inputs * INPUT_MICROS_PER_TOKEN + outputs * OUTPUT_MICROS_PER_TOKEN


def _first_text_block(response: Any) -> str | None:
    """Response content is a list of typed blocks; take the first text one."""
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text":
            return str(getattr(block, "text", ""))
    return None


class ScriptedClient:
    """Returns canned payloads in order, then repeats the last one.

    Lets the planner, the re-plan loop and the fallback path be tested exactly,
    with no network and no spend.
    """

    def __init__(
        self, payloads: Sequence[dict[str, Any] | None], *, cost_micros: int = 900
    ) -> None:
        self._payloads = list(payloads)
        self._cost = cost_micros
        self.calls = 0
        self.prompts: list[str] = []

    def propose(self, *, system: str, prompt: str, schema: dict[str, Any]) -> ModelReply:
        self.prompts.append(prompt)
        index = min(self.calls, len(self._payloads) - 1) if self._payloads else -1
        self.calls += 1
        if index < 0:
            return ModelReply.failure("no scripted payload", model="scripted", latency_ms=0)
        payload = self._payloads[index]
        if payload is None:
            return ModelReply.failure("scripted failure", model="scripted", latency_ms=1)
        return ModelReply(
            payload=payload,
            ok=True,
            error=None,
            model="scripted",
            cost_micros=self._cost,
            latency_ms=1,
        )


class UnavailableClient:
    """Always fails. Exercises the deterministic fallback path."""

    def __init__(self, error: str = "model_unavailable") -> None:
        self._error = error
        self.calls = 0

    def propose(self, *, system: str, prompt: str, schema: dict[str, Any]) -> ModelReply:
        self.calls += 1
        return ModelReply.failure(self._error, model="unavailable", latency_ms=0)
