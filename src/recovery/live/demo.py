"""The stand-in agent the control room runs on stage.

A demo whose agent proposes only legal actions would show a policy engine that
never speaks -- and the policy engine is the thing worth watching. So this
stand-in makes the mistakes a real model makes, in a fixed order, inside thirty
seconds.

**It is a stand-in, not a mock of the outcome.** It reads the same prompt
:func:`~recovery.agent.planner.build_prompt` hands a real model, returns through
the same ``ModelReply``, and is validated by the same schema and gated by the
same eight gates. What is fixed is only *which* mistake happens when, so the
refusal-and-re-plan beat lands on cue instead of whenever a live model happens
to overreach. Live runs take the identical path: ``docs/RESULTS.md`` records 158
refusals and 17 schema rejections in one, unprompted.

:class:`~recovery.agent.client.ScriptedClient` was rejected for this. Its cursor
advances globally, so with cases interleaved every planner call after the third
returned the same payload -- one case accumulated 64 events proposing the same
refused debit. A per-case story has to be a function of the case, and the prompt
is where the case is.

Three beats, each reproducing a failure mode the audit report shows for real:

1. **the overreach** -- a debit proposed before the statutory notice exists,
   refused by ``gate_mandate``, then re-planned onto the remedy the refusal
   names;
2. **the malformed proposal** -- a message action naming no template, rejected
   by schema validation before the policy engine ever sees it, and absorbed by
   the deterministic fallback;
3. **the wait** -- an outage refusal answered by waiting rather than hammering.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from recovery.agent.client import ModelReply

DEMO_SEED = 20260827
DEMO_MODEL = "demo-stand-in"

_NO_NOTICE = "pre-debit notice: none sent"
_HARD_DECLINE = "decline class:  hard"
_UNKNOWN_DECLINE = "decline class:  unknown"
_REPAIR_REQUESTED = "instrument update requested: True"
_REFUSED = "was REFUSED:"

_AFA_FREE_CEILING_RUPEES = 15_000
"""RBI's additional-factor ceiling for e-mandate debits. The higher
₹1,00,000 carve-out applies to insurance, mutual funds and card bills, and
the prompt does not expose the mandate category -- so the conservative
reading is the right one here. Proposing a link where a debit would have
been permitted costs a message; the reverse costs a refusal loop."""

_AMOUNT_PATTERN = re.compile(r"amount:\s+(?:Rs|\u20b9)\s*([\d,]+)")


def _amount_rupees(prompt: str) -> int | None:
    """The case amount as whole rupees, read off the prompt."""
    match = _AMOUNT_PATTERN.search(prompt)
    return int(match.group(1).replace(",", "")) if match else None


def _reply(payload: dict[str, Any]) -> ModelReply:
    """Wrap a proposal in the same envelope a provider adapter returns.

    Token counts are representative of `gpt-4.1-mini` on this prompt so the
    console's usage panel shows plausible magnitudes rather than zeroes; cost is
    zero because nothing was bought, and a demo that displayed invented spend
    would be inventing the one number this project refuses to invent.
    """
    return ModelReply(
        payload=payload,
        ok=True,
        error=None,
        model=DEMO_MODEL,
        input_tokens=310,
        output_tokens=88,
        cost_micros=0,
        latency_ms=2,
    )


def _proposal(
    action: str,
    *,
    channel: str = "none",
    template_id: str | None = None,
    delay_hours: int = 0,
    diagnosis: str,
    rationale: str,
    confidence: float,
) -> dict[str, Any]:
    return {
        "action": action,
        "channel": channel,
        "template_id": template_id,
        "delay_hours": delay_hours,
        "diagnosis": diagnosis,
        "confidence": confidence,
        "rationale": rationale,
    }


class DemoClient:
    """A deterministic stand-in that answers the prompt it is given."""

    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    def propose(self, *, system: str, prompt: str, schema: dict[str, Any]) -> ModelReply:
        self.calls += 1
        self.prompts.append(prompt)
        if _REFUSED in prompt:
            return _reply(self._replan(prompt))
        return _reply(self._opening_move(prompt))

    @staticmethod
    def _opening_move(prompt: str) -> dict[str, Any]:
        """What it reaches for before anything has told it no."""
        if _UNKNOWN_DECLINE in prompt:
            # Beat 2: an unmapped code, so it reaches for a human -- and names a
            # message action with no template. Schema validation rejects this
            # before the policy engine is ever consulted, and the deterministic
            # fallback picks the case up. Fired on the unknown class because
            # that is what routes to the agent tail (runner._tail_subtype), so
            # the beat is reachable rather than theoretical.
            return _proposal(
                "escalate",
                channel="sms",
                template_id=None,
                diagnosis="The decline code is not one I recognise; a human should look at it.",
                rationale="Escalate rather than guess at an uncharacterised failure.",
                confidence=0.41,
            )
        if _HARD_DECLINE in prompt:
            if _REPAIR_REQUESTED in prompt:
                # Asking twice is not twice as persuasive. Without this the case
                # re-proposes the same request on every planner call until it
                # hits max_escalation, which floods the DECIDE lane with one
                # case and reads as a stuck system rather than a patient one.
                return _proposal(
                    "wait",
                    delay_hours=24,
                    diagnosis="A replacement instrument has been requested and not yet supplied.",
                    rationale="Wait for the customer to act rather than repeating the ask.",
                    confidence=0.63,
                )
            return _proposal(
                "request_instrument_update",
                channel="sms",
                template_id="RP_INSTRUMENT_01",
                diagnosis="The instrument cannot succeed; only a replacement can recover this.",
                rationale="No debit can work on a dead instrument; ask for a new one.",
                confidence=0.66,
            )
        if _NO_NOTICE in prompt:
            # Beat 1: the overreach. RBI requires a notice before each debit.
            return _proposal(
                "retry_debit",
                diagnosis="Charge failed on a live mandate; the instrument looks retryable.",
                rationale="Retry now while the balance may have been topped up.",
                confidence=0.62,
            )
        amount = _amount_rupees(prompt)
        if amount is not None and amount > _AFA_FREE_CEILING_RUPEES:
            # Checked only once a notice exists, so the overreach above still
            # happens on every fresh case -- that refusal is the loop worth
            # watching. Reaching for a debit *again* on an over-ceiling case is
            # the one that loops: refused, link sent, refused identically next
            # call. Reading the amount off the prompt is what stops it.
            return _REMEDIES["send_payment_link"]()
        return _proposal(
            "retry_debit",
            delay_hours=25,
            diagnosis="Notice has been served; the mandate permits an attempt once it matures.",
            rationale="Retry after the 24h notice window elapses.",
            confidence=0.58,
        )

    @staticmethod
    def _replan(prompt: str) -> dict[str, Any]:
        """Answer the refusal with the remedy it named.

        Every gate refusal carries a ``remediation`` -- an action that would
        unblock it -- and the prompt renders it as ``(unblocked by: X)``. Reading
        that field is the entire reason it exists: a re-plan becomes a lookup
        inside the bounded action menu rather than a guess at what the engine
        might accept next.

        One mechanism covers every code. An over-threshold debit is answered
        with a payment link because that is what ``gate_mandate`` names; a stale
        notice with a fresh notice; a spent attempt budget with ``stop``. None
        of those mappings are written here.
        """
        remedy = _named_remedy(prompt)
        if remedy is None:
            return _proposal(
                "wait",
                delay_hours=8,
                diagnosis="The action is not permitted in the present state, and the "
                "refusal named no remedy.",
                rationale="Wait and reconsider rather than re-proposing a refused action.",
                confidence=0.5,
            )
        return _REMEDIES.get(remedy, _REMEDIES["wait"])()


_REMEDY_PATTERN = re.compile(r"\(unblocked by: ([a-z_]+)\)")


def _named_remedy(prompt: str) -> str | None:
    """The remedy from the most recent refusal, if one was named."""
    matches = _REMEDY_PATTERN.findall(prompt)
    return matches[-1] if matches else None


_REMEDIES: dict[str, Callable[[], dict[str, Any]]] = {
    "send_predebit_notice": lambda: _proposal(
        "send_predebit_notice",
        channel="sms",
        template_id="RP_PREDEBIT_01",
        diagnosis="No debit is permitted until a current statutory notice has been served.",
        rationale="Serve the notice the refusal named, so a debit becomes schedulable.",
        confidence=0.74,
    ),
    "send_payment_link": lambda: _proposal(
        "send_payment_link",
        channel="sms",
        template_id="RP_PAYLINK_01",
        diagnosis="The amount exceeds the AFA-free ceiling, so an automatic debit cannot carry it.",
        rationale="A payment link lets the customer authenticate the amount themselves.",
        confidence=0.71,
    ),
    "request_instrument_update": lambda: _proposal(
        "request_instrument_update",
        channel="sms",
        template_id="RP_INSTRUMENT_01",
        diagnosis="The instrument is dead; no retry against it can succeed.",
        rationale="Instrument repair is the only route out of a hard decline.",
        confidence=0.68,
    ),
    "escalate": lambda: _proposal(
        "escalate",
        channel="sms",
        template_id="RP_DUNNING_01",
        diagnosis="This failure is not one the automated ladder can resolve.",
        rationale="Move it up the contact ladder rather than spending attempts blindly.",
        confidence=0.6,
    ),
    "stop": lambda: _proposal(
        "stop",
        diagnosis="The attempt budget for this instrument is spent.",
        rationale="No further debit is permitted; close the case rather than idle on it.",
        confidence=0.72,
    ),
    "wait": lambda: _proposal(
        "wait",
        delay_hours=6,
        diagnosis="The blocking condition clears on its own.",
        rationale="Wait it out rather than spending an attempt into a known failure.",
        confidence=0.66,
    ),
}
