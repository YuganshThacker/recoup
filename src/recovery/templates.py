"""The registered template catalogue.

On regulated Indian channels the copy is not ours to write: SMS content must be
pre-approved on a DLT platform, and WhatsApp business-initiated messages need
approved templates. So the system holds template *identities* and the variable
names each one expects, and never holds message text.

That is why a planner -- rules or model -- selects a ``template_id`` and binds
variables. There is no code path through which prose reaches a customer.
"""

from __future__ import annotations

from recovery.policy.actions import Channel
from recovery.policy.gates import MessageTemplate

PREDEBIT_NOTICE = MessageTemplate(
    template_id="RP_PREDEBIT_01",
    channel=Channel.SMS,
    # The five fields RBI requires a pre-debit notification to carry.
    required_variables=frozenset({"merchant", "amount", "debit_at", "mandate_ref", "reason"}),
    purpose="payment_recovery",
)

DUNNING_REMINDER = MessageTemplate(
    template_id="RP_DUNNING_01",
    channel=Channel.SMS,
    required_variables=frozenset({"merchant", "amount", "due_date"}),
    purpose="payment_recovery",
)

INSTRUMENT_UPDATE = MessageTemplate(
    template_id="RP_INSTRUMENT_01",
    channel=Channel.SMS,
    required_variables=frozenset({"merchant", "amount", "update_link"}),
    purpose="payment_recovery",
)

PAYMENT_LINK = MessageTemplate(
    template_id="RP_PAYLINK_01",
    channel=Channel.SMS,
    required_variables=frozenset({"merchant", "amount", "link", "expires_at"}),
    purpose="payment_recovery",
)

REGISTERED: dict[str, MessageTemplate] = {
    t.template_id: t for t in (PREDEBIT_NOTICE, DUNNING_REMINDER, INSTRUMENT_UPDATE, PAYMENT_LINK)
}


def bind_variables(template_id: str) -> dict[str, str]:
    """Bind every variable a template requires.

    Values come from the case ledger, never from a planner and never from a
    model -- a planner names a template, the system fills it. Centralised here
    so the rules path and the agent path cannot diverge in what they are
    permitted to put in front of a customer.

    In the simulator the bindings are placeholders keyed by variable name; a
    live integration substitutes merchant name, amount, mandate reference and
    debit time from the case record at send time.
    """
    template = REGISTERED.get(template_id)
    if template is None:
        raise KeyError(f"'{template_id}' is not a registered template")
    return {name: name for name in sorted(template.required_variables)}
