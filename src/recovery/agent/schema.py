"""The model's output contract.

A proposal is the only thing the model is allowed to emit, and its shape is what
makes the model safe to have in the loop at all:

* **There is no amount field.** Amounts come from the case ledger at execution
  time. A hallucinated figure has no field to travel in, so it cannot reach a
  debit -- this is a structural guarantee, not a validation rule that might be
  forgotten.
* **There is no message-text field, and no variable bindings either.** The model
  selects a registered ``template_id``; the system fills every variable from the
  ledger. On DLT-registered SMS the copy is not ours to write, so the model's
  entire influence over what a customer reads is *which* approved template is
  chosen.
* **The action is a closed enum.** The model picks from the bounded menu; it
  cannot invent a new kind of money movement.

Anything that fails validation is discarded and the deterministic planner takes
the turn. A malformed proposal costs a fallback, never a bad action.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from recovery.policy.actions import ActionKind, Channel

# The model may propose these. STOP and WAIT are included because "do nothing
# yet" and "give up" are real decisions that should be auditable when the model
# makes them, rather than side effects of proposing nothing.
PROPOSABLE_ACTIONS: tuple[ActionKind, ...] = (
    ActionKind.RETRY_DEBIT,
    ActionKind.SEND_PREDEBIT_NOTICE,
    ActionKind.SEND_REMINDER,
    ActionKind.SEND_PAYMENT_LINK,
    ActionKind.REQUEST_INSTRUMENT_UPDATE,
    ActionKind.ESCALATE,
    ActionKind.WAIT,
    ActionKind.STOP,
)

PROPOSABLE_CHANNELS: tuple[Channel, ...] = (
    Channel.NONE,
    Channel.EMAIL,
    Channel.SMS,
    Channel.WHATSAPP_UTILITY,
)

MAX_DELAY_HOURS = 24 * 30
MAX_TEXT_LENGTH = 600


@dataclass(frozen=True, slots=True)
class AgentProposal:
    """A validated proposal from the model."""

    action: ActionKind
    channel: Channel
    template_id: str | None
    delay_hours: int
    diagnosis: str
    confidence: float
    rationale: str


class InvalidProposal(Exception):
    """The model returned something outside the contract."""


def proposal_schema(template_ids: list[str]) -> dict[str, Any]:
    """JSON schema for the structured output.

    ``additionalProperties: false`` plus a fully enumerated set of fields means
    the model cannot smuggle an extra key past the schema -- notably not an
    amount and not message text.
    """
    return {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [a.value for a in PROPOSABLE_ACTIONS],
                "description": "Which bounded action to take next.",
            },
            "channel": {
                "type": "string",
                "enum": [c.value for c in PROPOSABLE_CHANNELS],
                "description": "Delivery channel; 'none' for actions that send nothing.",
            },
            # anyOf rather than a nullable type carrying null inside its enum.
            # Both forms are defensible, but only this one is unambiguous under
            # every provider's strict-schema mode, and a schema that is rejected
            # at the API boundary loses the guarantee it exists to provide.
            "template_id": {
                "anyOf": [
                    {"type": "string", "enum": list(template_ids)},
                    {"type": "null"},
                ],
                "description": "A registered template id, or null when no message is sent.",
            },
            "delay_hours": {
                "type": "integer",
                "minimum": 0,
                "maximum": MAX_DELAY_HOURS,
                "description": "Hours from now to take the action.",
            },
            "diagnosis": {
                "type": "string",
                "description": "Root-cause reading of why the charge failed.",
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "Confidence in the diagnosis.",
            },
            "rationale": {
                "type": "string",
                "description": "Why this action, at this time, on this channel.",
            },
        },
        "required": [
            "action",
            "channel",
            "template_id",
            "delay_hours",
            "diagnosis",
            "confidence",
            "rationale",
        ],
        "additionalProperties": False,
    }


def _require(payload: dict[str, Any], key: str) -> Any:
    if key not in payload:
        raise InvalidProposal(f"missing required field '{key}'")
    return payload[key]


def _text(payload: dict[str, Any], key: str) -> str:
    value = _require(payload, key)
    if not isinstance(value, str):
        raise InvalidProposal(f"'{key}' must be a string, got {type(value).__name__}")
    return value[:MAX_TEXT_LENGTH]


def validate(payload: dict[str, Any], *, known_templates: frozenset[str]) -> AgentProposal:
    """Validate a raw model payload, or raise :class:`InvalidProposal`.

    Revalidated here even though the schema was sent with the request. Schema
    enforcement is a property of the request we cannot verify from inside this
    process, and this object is about to influence a money action -- so it is
    checked where it is used, not where it was asked for.
    """
    if not isinstance(payload, dict):
        raise InvalidProposal(f"expected an object, got {type(payload).__name__}")

    raw_action = _text(payload, "action")
    try:
        action = ActionKind(raw_action)
    except ValueError as exc:
        raise InvalidProposal(f"'{raw_action}' is not a known action") from exc
    if action not in PROPOSABLE_ACTIONS:
        raise InvalidProposal(f"'{action.value}' is not proposable")

    raw_channel = _text(payload, "channel")
    try:
        channel = Channel(raw_channel)
    except ValueError as exc:
        raise InvalidProposal(f"'{raw_channel}' is not a known channel") from exc
    if channel not in PROPOSABLE_CHANNELS:
        raise InvalidProposal(f"'{channel.value}' is not proposable")

    template_id = _require(payload, "template_id")
    if template_id is not None:
        if not isinstance(template_id, str):
            raise InvalidProposal("'template_id' must be a string or null")
        if template_id not in known_templates:
            raise InvalidProposal(f"'{template_id}' is not a registered template")

    delay = _require(payload, "delay_hours")
    if isinstance(delay, bool) or not isinstance(delay, int):
        raise InvalidProposal("'delay_hours' must be an integer")
    if not 0 <= delay <= MAX_DELAY_HOURS:
        raise InvalidProposal(f"'delay_hours' out of range: {delay}")

    confidence = _require(payload, "confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, int | float):
        raise InvalidProposal("'confidence' must be a number")
    if not 0.0 <= float(confidence) <= 1.0:
        raise InvalidProposal(f"'confidence' out of range: {confidence}")

    if action.contacts_customer and template_id is None:
        raise InvalidProposal(f"'{action.value}' sends a message but named no template")

    return AgentProposal(
        action=action,
        channel=channel,
        template_id=template_id,
        delay_hours=delay,
        diagnosis=_text(payload, "diagnosis"),
        confidence=float(confidence),
        rationale=_text(payload, "rationale"),
    )
