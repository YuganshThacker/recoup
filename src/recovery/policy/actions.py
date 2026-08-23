"""The bounded action menu.

"Bounded recovery workflow" means the system can do these things and nothing
else. The menu is a closed enum rather than an open instruction space, which is
what lets the policy engine reason exhaustively about what is permitted and
what lets the model choose without being able to invent a novel money movement.

A proposed action never carries an amount. Amounts come from the case ledger at
execution time. This is structural, not stylistic: it means a hallucinated
figure cannot reach a debit, because there is no field for one to travel in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from recovery.domain.events import Actor


class ActionKind(StrEnum):
    """Everything the recovery system is capable of doing."""

    RETRY_DEBIT = "retry_debit"
    """Charge the existing mandate again."""

    SEND_PREDEBIT_NOTICE = "send_predebit_notice"
    """RBI-mandated notification ahead of a mandate debit."""

    SEND_REMINDER = "send_reminder"
    """Outbound message about the outstanding amount."""

    SEND_PAYMENT_LINK = "send_payment_link"
    """Razorpay payment link so the customer can pay by another route."""

    REQUEST_INSTRUMENT_UPDATE = "request_instrument_update"
    """Ask for a new card or a fresh mandate. The only path out of a HARD decline."""

    ESCALATE = "escalate"
    """Move up the contact ladder."""

    WAIT = "wait"
    """Do nothing now and reconsider later. An explicit choice, so it is auditable."""

    STOP = "stop"
    """Terminate the case."""

    @property
    def moves_money(self) -> bool:
        """Does executing this attempt a debit?"""
        return self is ActionKind.RETRY_DEBIT

    @property
    def contacts_customer(self) -> bool:
        """Does executing this send something to a person?

        Pre-debit notices count: they are messages to a customer and are
        therefore subject to consent and template rules. They are exempt from
        quiet hours and cooldown only because withholding a legally required
        notice to satisfy an internal comfort rule would be the worse failure.
        """
        return self in _CONTACT_ACTIONS


_CONTACT_ACTIONS: frozenset[ActionKind] = frozenset(
    {
        ActionKind.SEND_PREDEBIT_NOTICE,
        ActionKind.SEND_REMINDER,
        ActionKind.SEND_PAYMENT_LINK,
        ActionKind.REQUEST_INSTRUMENT_UPDATE,
        ActionKind.ESCALATE,
    }
)


class Channel(StrEnum):
    NONE = "none"
    EMAIL = "email"
    SMS = "sms"
    WHATSAPP_UTILITY = "whatsapp_utility"
    WHATSAPP_MARKETING = "whatsapp_marketing"
    VOICE = "voice"

    @property
    def requires_registered_template(self) -> bool:
        """Channels where free-form copy is not permitted.

        Under TRAI's TCCCPR/DLT regime commercial SMS must use pre-registered
        templates, and WhatsApp requires approved templates for
        business-initiated messages. Free text is not an option on either, which
        is why the model selects a template id and binds variables rather than
        writing prose.
        """
        return self in {
            Channel.SMS,
            Channel.WHATSAPP_UTILITY,
            Channel.WHATSAPP_MARKETING,
            Channel.VOICE,
        }


@dataclass(frozen=True, slots=True)
class ProposedAction:
    """An action awaiting a policy decision.

    Deliberately amount-free. ``template_id`` and ``variables`` carry message
    content by reference, never by value, so nothing here can assert a figure.
    """

    kind: ActionKind
    channel: Channel = Channel.NONE
    template_id: str | None = None
    variables: dict[str, str] = field(default_factory=dict)
    proposed_by: Actor = Actor.RULES
    rationale: str = ""

    def describe(self) -> str:
        """One line for the audit ledger."""
        where = f" via {self.channel.value}" if self.channel is not Channel.NONE else ""
        template = f" [{self.template_id}]" if self.template_id else ""
        return f"{self.kind.value}{where}{template}"
