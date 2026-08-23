"""The eight gates.

Each gate is a pure function of ``(action, context) -> GateResult``. No I/O, no
clock of its own, no hidden state -- everything a gate needs arrives in the
context, which is what makes the whole compliance surface unit-testable without
a database or a fake time library.

Gates are ordered from "this can never be allowed" to "this is allowed but not
worth it", so a reader scanning a refusal list sees the most fundamental
objection first. The engine still evaluates all of them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from recovery.domain.case import RecoveryCase
from recovery.domain.failure import DeclineClass
from recovery.domain.money import Paise, paise
from recovery.policy import constants as K
from recovery.policy.actions import ActionKind, Channel, ProposedAction
from recovery.policy.decision import GateName, GateResult, RefusalCode


@dataclass(frozen=True, slots=True)
class MessageTemplate:
    """A DLT/WhatsApp-registered template.

    Registration is what makes a message sendable. The system holds template
    identities and required variable names; it never holds or generates copy,
    because on regulated Indian channels copy is not ours to write.
    """

    template_id: str
    channel: Channel
    required_variables: frozenset[str]
    purpose: str


@dataclass(frozen=True, slots=True)
class PolicyContext:
    """Everything the gates need, gathered once by the caller."""

    case: RecoveryCase
    now: datetime

    # consent (DPDP: purpose-scoped, per channel)
    consented_channels: frozenset[Channel] = frozenset()
    consented_purposes: frozenset[str] = frozenset()

    # suppression
    dispute_open: bool = False
    do_not_contact: bool = False
    subscription_active: bool = True
    promise_to_pay_until: datetime | None = None

    # mandate
    mandate_active: bool = True
    predebit_notice_sent_at: datetime | None = None
    predebit_opted_out: bool = False
    mandate_category: str = "general"
    attempt_carries_afa: bool = False

    # attempt budget
    attempts_last_30d: int = 0
    card_network: str | None = None

    # downtime
    downtime_active: bool = False
    downtime_expected_end: datetime | None = None

    # contact history
    last_contact_at: datetime | None = None

    templates: dict[str, MessageTemplate] = field(default_factory=dict)

    @property
    def afa_exempt_limit(self) -> Paise:
        """AFA-free ceiling for this mandate's category."""
        if self.mandate_category in K.AFA_HIGH_LIMIT_CATEGORIES:
            return K.AFA_EXEMPT_LIMIT_HIGH
        return K.AFA_EXEMPT_LIMIT


# --- 1. consent ------------------------------------------------------------


def gate_consent(action: ProposedAction, ctx: PolicyContext) -> GateResult:
    """DPDP: consent must be free, specific, informed, and scoped to a purpose."""
    g = GateName.CONSENT
    if not action.kind.contacts_customer:
        return GateResult.allow(g, "action does not contact the customer")
    if action.channel not in ctx.consented_channels:
        return GateResult.refuse(
            g,
            RefusalCode.NO_CONSENT,
            f"no consent on record for {action.channel.value}",
            remediation=ActionKind.WAIT,
        )
    template = ctx.templates.get(action.template_id or "")
    if template and template.purpose not in ctx.consented_purposes:
        return GateResult.refuse(
            g,
            RefusalCode.CONSENT_PURPOSE_MISMATCH,
            f"consent does not cover purpose '{template.purpose}'",
        )
    return GateResult.allow(g, f"consented for {action.channel.value}")


# --- 2. suppression --------------------------------------------------------


def gate_suppression(action: ProposedAction, ctx: PolicyContext) -> GateResult:
    """Situations where pursuing recovery is wrong regardless of legality."""
    g = GateName.SUPPRESSION
    if action.kind in (ActionKind.WAIT, ActionKind.STOP):
        return GateResult.allow(g, "no-op action")
    if not ctx.subscription_active:
        return GateResult.refuse(g, RefusalCode.SUBSCRIPTION_CANCELLED, "subscription is cancelled")
    if ctx.dispute_open:
        return GateResult.refuse(
            g, RefusalCode.SUPPRESSED_DISPUTE, "an open dispute covers this invoice"
        )
    if ctx.do_not_contact:
        return GateResult.refuse(
            g, RefusalCode.SUPPRESSED_DO_NOT_CONTACT, "customer is on the do-not-contact list"
        )
    if ctx.promise_to_pay_until and ctx.now < ctx.promise_to_pay_until:
        return GateResult.refuse(
            g,
            RefusalCode.SUPPRESSED_PROMISE_TO_PAY,
            f"promise to pay stands until {ctx.promise_to_pay_until.isoformat()}",
            retry_after=ctx.promise_to_pay_until,
        )
    return GateResult.allow(g, "no suppression in force")


# --- 3. mandate ------------------------------------------------------------


def _decline_class_blocks_retry(ctx: PolicyContext) -> GateResult | None:
    """Refuse debits the decline taxonomy says cannot succeed."""
    g = GateName.MANDATE
    klass = ctx.case.decline_class
    if klass is DeclineClass.HARD:
        return GateResult.refuse(
            g,
            RefusalCode.HARD_DECLINE_NO_RETRY,
            f"'{ctx.case.decline_reason}' is a hard decline; the instrument cannot succeed",
            remediation=ActionKind.REQUEST_INSTRUMENT_UPDATE,
        )
    if klass is DeclineClass.DOWNTIME and ctx.downtime_active:
        return GateResult.refuse(
            g,
            RefusalCode.DOWNTIME_ACTIVE,
            "issuer or gateway downtime is in progress",
            retry_after=ctx.downtime_expected_end,
            remediation=ActionKind.WAIT,
        )
    if klass is DeclineClass.UNKNOWN:
        return GateResult.refuse(
            g,
            RefusalCode.UNCLASSIFIED_DECLINE_NO_RETRY,
            f"'{ctx.case.decline_reason}' is unmapped; not spending an attempt on it",
            remediation=ActionKind.ESCALATE,
        )
    return None


def _predebit_notice_blocks_retry(ctx: PolicyContext) -> GateResult | None:
    """RBI e-mandate: a debit needs a matured, un-opted-out, fresh notice."""
    g = GateName.MANDATE
    sent = ctx.predebit_notice_sent_at
    if sent is None:
        return GateResult.refuse(
            g,
            RefusalCode.PREDEBIT_NOTICE_REQUIRED,
            "no pre-debit notice sent for this attempt",
            remediation=ActionKind.SEND_PREDEBIT_NOTICE,
        )
    if ctx.predebit_opted_out:
        # The customer declined this specific debit. That is a stopping signal,
        # not an obstacle to route around: no remediation, no retry_after.
        return GateResult.refuse(
            g, RefusalCode.PREDEBIT_OPTED_OUT, "customer opted out of this debit"
        )
    matures_at = sent + K.PREDEBIT_NOTICE_LEAD
    if ctx.now < matures_at:
        return GateResult.refuse(
            g,
            RefusalCode.PREDEBIT_NOTICE_NOT_MATURE,
            f"notice sent {sent.isoformat()}; 24h lead matures {matures_at.isoformat()}",
            retry_after=matures_at,
        )
    if ctx.now > sent + K.PREDEBIT_NOTICE_MAX_AGE:
        return GateResult.refuse(
            g,
            RefusalCode.PREDEBIT_NOTICE_STALE,
            "notice is too old to describe this debit",
            remediation=ActionKind.SEND_PREDEBIT_NOTICE,
        )
    return None


def gate_mandate(action: ProposedAction, ctx: PolicyContext) -> GateResult:
    """Everything that governs whether a debit may be attempted at all."""
    g = GateName.MANDATE
    if not action.kind.moves_money:
        return GateResult.allow(g, "action does not move money")
    if not ctx.mandate_active:
        return GateResult.refuse(g, RefusalCode.MANDATE_REVOKED, "mandate is no longer active")

    for check in (_decline_class_blocks_retry, _predebit_notice_blocks_retry):
        refusal = check(ctx)
        if refusal is not None:
            return refusal

    limit = ctx.afa_exempt_limit
    if ctx.case.amount > limit and not ctx.attempt_carries_afa:
        return GateResult.refuse(
            g,
            RefusalCode.AFA_REQUIRED_ABOVE_THRESHOLD,
            f"amount exceeds the AFA-free ceiling for category '{ctx.mandate_category}'",
            # The product answer to an over-threshold debit is to let the
            # customer authenticate, which a payment link does.
            remediation=ActionKind.SEND_PAYMENT_LINK,
        )
    return GateResult.allow(g, "mandate valid, notice matured, within AFA ceiling")


# --- 4. attempt budget -----------------------------------------------------


def gate_attempt_budget(action: ProposedAction, ctx: PolicyContext) -> GateResult:
    """Caps on reattempts. Network ceilings carry per-attempt fees, so this is
    cost control as much as compliance."""
    g = GateName.ATTEMPT_BUDGET
    if not action.kind.moves_money:
        return GateResult.allow(g, "action does not consume attempt budget")

    if ctx.case.attempt_count >= K.INTERNAL_MAX_ATTEMPTS_PER_CASE:
        return GateResult.refuse(
            g,
            RefusalCode.INTERNAL_ATTEMPT_CAP,
            f"case has used {ctx.case.attempt_count} of "
            f"{K.INTERNAL_MAX_ATTEMPTS_PER_CASE} permitted attempts",
        )

    network_limit = K.NETWORK_30D_ATTEMPT_LIMITS.get(
        (ctx.card_network or "").lower(), K.DEFAULT_30D_ATTEMPT_LIMIT
    )
    if ctx.attempts_last_30d >= network_limit:
        return GateResult.refuse(
            g,
            RefusalCode.NETWORK_ATTEMPT_CAP,
            f"{ctx.attempts_last_30d} attempts in 30d reaches the "
            f"{ctx.card_network or 'default'} ceiling of {network_limit}",
        )
    return GateResult.allow(
        g, f"attempt {ctx.case.attempt_count + 1} of {K.INTERNAL_MAX_ATTEMPTS_PER_CASE}"
    )


# --- 5. quiet hours --------------------------------------------------------


def _next_window_open(now: datetime) -> datetime:
    """Next 08:00 in the customer's timezone."""
    local = now.astimezone(K.CUSTOMER_TIMEZONE)
    candidate = local.replace(hour=K.CONTACT_WINDOW_OPEN_HOUR, minute=0, second=0, microsecond=0)
    if local.hour >= K.CONTACT_WINDOW_CLOSE_HOUR:
        candidate += timedelta(days=1)
    return candidate


def gate_quiet_hours(action: ProposedAction, ctx: PolicyContext) -> GateResult:
    """RBI Fair Practices Code: contact only between 08:00 and 19:00 local.

    Pre-debit notices are exempt. They are a legally required disclosure with a
    hard lead time, and withholding one to satisfy a contact-hours rule would
    mean either a late notice or a missed debit -- the worse outcome for the
    customer, who loses their opt-out window.
    """
    g = GateName.QUIET_HOURS
    if not action.kind.contacts_customer:
        return GateResult.allow(g, "action does not contact the customer")
    if action.kind is ActionKind.SEND_PREDEBIT_NOTICE:
        return GateResult.allow(g, "statutory notice, exempt from contact hours")

    local = ctx.now.astimezone(K.CUSTOMER_TIMEZONE)
    if K.CONTACT_WINDOW_OPEN_HOUR <= local.hour < K.CONTACT_WINDOW_CLOSE_HOUR:
        return GateResult.allow(g, f"{local:%H:%M} IST is inside the contact window")
    return GateResult.refuse(
        g,
        RefusalCode.OUTSIDE_CONTACT_HOURS,
        f"{local:%H:%M} IST is outside 08:00-19:00",
        retry_after=_next_window_open(ctx.now),
    )


# --- 6. cooldown -----------------------------------------------------------


def gate_cooldown(action: ProposedAction, ctx: PolicyContext) -> GateResult:
    """Minimum gap between outbound contacts on the same case."""
    g = GateName.COOLDOWN
    if not action.kind.contacts_customer:
        return GateResult.allow(g, "action does not contact the customer")
    if action.kind is ActionKind.SEND_PREDEBIT_NOTICE:
        return GateResult.allow(g, "statutory notice, exempt from cooldown")
    if ctx.last_contact_at is None:
        return GateResult.allow(g, "no prior contact on this case")

    ready_at = ctx.last_contact_at + K.CONTACT_COOLDOWN
    if ctx.now < ready_at:
        return GateResult.refuse(
            g,
            RefusalCode.COOLDOWN_ACTIVE,
            f"last contact {ctx.last_contact_at.isoformat()}; cooldown ends {ready_at.isoformat()}",
            retry_after=ready_at,
        )
    return GateResult.allow(g, "cooldown satisfied")


# --- 7. template -----------------------------------------------------------


def gate_template(action: ProposedAction, ctx: PolicyContext) -> GateResult:
    """TCCCPR/DLT and WhatsApp: registered templates only, variables only.

    This gate is why the model never writes copy. It selects a registered
    template and binds variables; anything else cannot pass.
    """
    g = GateName.TEMPLATE
    if not action.kind.contacts_customer:
        return GateResult.allow(g, "action sends no message")
    if not action.channel.requires_registered_template:
        return GateResult.allow(g, f"{action.channel.value} does not require registration")

    if action.template_id is None:
        return GateResult.refuse(
            g, RefusalCode.TEMPLATE_REQUIRED, f"{action.channel.value} requires a template id"
        )
    template = ctx.templates.get(action.template_id)
    if template is None or template.channel is not action.channel:
        return GateResult.refuse(
            g,
            RefusalCode.TEMPLATE_NOT_REGISTERED,
            f"'{action.template_id}' is not registered for {action.channel.value}",
        )
    supplied = frozenset(action.variables)
    if supplied != template.required_variables:
        missing = sorted(template.required_variables - supplied)
        extra = sorted(supplied - template.required_variables)
        return GateResult.refuse(
            g,
            RefusalCode.TEMPLATE_VARIABLE_MISMATCH,
            f"variables missing={missing} unexpected={extra}",
        )
    return GateResult.allow(g, f"'{template.template_id}' bound with {len(supplied)} variables")


# --- 8. channel economics --------------------------------------------------


def expected_recovery(ctx: PolicyContext) -> Paise:
    """Expected recoverable amount from one more action, in paise.

    Decimal throughout, floored to whole paise. The prior is an estimate seeded
    from published benchmarks and replaced by measured rates after the first
    batch; the decay reflects that each successive attempt returns less.
    """
    prior = K.BASE_RECOVERY_PRIOR.get(ctx.case.decline_class.value, Decimal("0.25"))
    decayed = prior * (K.ATTEMPT_DECAY**ctx.case.attempt_count)
    return paise(int(Decimal(int(ctx.case.amount)) * decayed))


def gate_channel_economics(action: ProposedAction, ctx: PolicyContext) -> GateResult:
    """Refuse actions that cost more than they can plausibly recover.

    A marketing-category WhatsApp message costs roughly five times a utility
    one, and a voice call an order of magnitude more again. Without this gate an
    escalation ladder will happily spend more chasing a small invoice than the
    invoice is worth.
    """
    g = GateName.CHANNEL_ECONOMICS
    if not action.kind.contacts_customer:
        return GateResult.allow(g, "action has no channel cost")

    cost = K.CHANNEL_COST.get(action.channel.value, paise(0))
    if cost == 0:
        return GateResult.allow(g, f"{action.channel.value} is free")

    value = expected_recovery(ctx)
    if value <= cost:
        return GateResult.refuse(
            g,
            RefusalCode.NEGATIVE_EXPECTED_VALUE,
            f"expected recovery {int(value)}p does not cover "
            f"{action.channel.value} at {int(cost)}p",
            remediation=ActionKind.STOP,
        )
    return GateResult.allow(g, f"expected {int(value)}p against {int(cost)}p cost")


ALL_GATES = (
    gate_consent,
    gate_suppression,
    gate_mandate,
    gate_attempt_budget,
    gate_quiet_hours,
    gate_cooldown,
    gate_template,
    gate_channel_economics,
)
