"""Model client boundary: the protocol, the reply type, and the test doubles.

Provider adapters live in sibling modules (:mod:`recovery.agent.anthropic_client`,
:mod:`recovery.agent.openai_client`) and import their SDKs lazily, so this
module -- and everything that depends only on it -- runs with no third-party
packages installed at all.

Two properties every adapter must hold, because the system's safety rests on
them rather than on the model behaving:

* **No failure raises.** Timeouts, refusals, malformed JSON and outages all
  return a :class:`ModelReply` with ``ok=False``. A model problem degrades the
  system to its rules path; it never stops a batch.
* **Tokens are always reported.** Under a free daily tier tokens, not rupees,
  are the scarce resource, so they are the primary unit and price is derived.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_TOKENS = 1024
DEFAULT_MAX_RETRIES = 2


@dataclass(frozen=True, slots=True)
class ModelReply:
    """One model call: what came back, what it consumed, whether it worked."""

    payload: dict[str, Any] | None
    ok: bool
    error: str | None
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_micros: int = 0
    """Micro-dollars, where the provider's pricing is known. Zero means either
    a failed call or an unpriced tier -- read ``total_tokens`` instead."""

    latency_ms: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @staticmethod
    def failure(error: str, *, model: str, latency_ms: int = 0) -> ModelReply:
        return ModelReply(payload=None, ok=False, error=error, model=model, latency_ms=latency_ms)


class LLMClient(Protocol):
    """What the agent planner needs from a model."""

    def propose(self, *, system: str, prompt: str, schema: dict[str, Any]) -> ModelReply: ...


class BudgetedClient:
    """Wraps a client with a hard token ceiling.

    A daily free-token allowance is a real operational constraint, and a batch
    can exhaust one without noticing. Once the ceiling is reached this returns a
    failure rather than calling out, which drops the planner onto its
    deterministic floor: the batch still completes, and the report shows how
    many cases ran without the model.

    The check happens before a call, against tokens already spent, so the
    ceiling can be overshot by at most one call's worth -- a request's cost is
    not known until it returns. Set the ceiling below a hard quota rather than
    at it.
    """

    def __init__(self, inner: LLMClient, *, max_total_tokens: int) -> None:
        self._inner = inner
        self._max_total_tokens = max_total_tokens
        self.tokens_used = 0
        self.refused_for_budget = 0

    @property
    def exhausted(self) -> bool:
        return self.tokens_used >= self._max_total_tokens

    def propose(self, *, system: str, prompt: str, schema: dict[str, Any]) -> ModelReply:
        if self.exhausted:
            self.refused_for_budget += 1
            return ModelReply.failure("token_budget_exhausted", model="budgeted")
        reply = self._inner.propose(system=system, prompt=prompt, schema=schema)
        self.tokens_used += reply.total_tokens
        return reply


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
            return ModelReply.failure("no scripted payload", model="scripted")
        payload = self._payloads[index]
        if payload is None:
            return ModelReply.failure("scripted failure", model="scripted", latency_ms=1)
        return ModelReply(
            payload=payload,
            ok=True,
            error=None,
            model="scripted",
            input_tokens=300,
            output_tokens=120,
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
        return ModelReply.failure(self._error, model="unavailable")
