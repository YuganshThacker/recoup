"""Policy engine tests.

The compliance envelope is the part of this system that must not be wrong, so
each gate is tested in isolation and then the sequence that matters most --
a debit blocked by the RBI notice rule, remediated, and finally permitted -- is
tested end to end.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from recovery.domain.case import RecoveryCase
from recovery.domain.events import InMemoryLedger, Ledger
from recovery.domain.failure import DeclineClass, PaymentMethod
from recovery.domain.money import paise
from recovery.policy.actions import ActionKind, Channel, ProposedAction
from recovery.policy.decision import RefusalCode
from recovery.policy.engine import PolicyEngine, remediation_plan
from recovery.policy.gates import (
    MessageTemplate,
    PolicyContext,
    gate_attempt_budget,
    gate_channel_economics,
    gate_consent,
    gate_cooldown,
    gate_mandate,
    gate_quiet_hours,
    gate_suppression,
    gate_template,
)

# 10:30 IST -- comfortably inside the 08:00-19:00 contact window.
NOON_IST = datetime(2026, 8, 23, 5, 0, tzinfo=UTC)

REMINDER_SMS = MessageTemplate(
    template_id="RP_DUNNING_01",
    channel=Channel.SMS,
    required_variables=frozenset({"merchant", "amount", "due_date"}),
    purpose="payment_recovery",
)
NOTICE_SMS = MessageTemplate(
    template_id="RP_PREDEBIT_01",
    channel=Channel.SMS,
    required_variables=frozenset({"merchant", "amount", "debit_at", "mandate_ref", "reason"}),
    purpose="payment_recovery",
)


def make_case(
    *,
    amount: int = 49900,
    decline_class: DeclineClass = DeclineClass.SOFT,
    reason: str = "insufficient_funds",
    attempts: int = 0,
) -> RecoveryCase:
    return RecoveryCase(
        case_id="case_1",
        subscription_id="sub_1",
        invoice_id="inv_1",
        customer_id="cust_1",
        amount=paise(amount),
        method=PaymentMethod.EMANDATE,
        decline_reason=reason,
        decline_class=decline_class,
        attempt_count=attempts,
    )


def make_ctx(**overrides: object) -> PolicyContext:
    """Context that passes every gate unless an override breaks one."""
    defaults: dict[str, object] = {
        "case": make_case(),
        "now": NOON_IST,
        "consented_channels": frozenset({Channel.SMS, Channel.EMAIL, Channel.WHATSAPP_UTILITY}),
        "consented_purposes": frozenset({"payment_recovery"}),
        "templates": {t.template_id: t for t in (REMINDER_SMS, NOTICE_SMS)},
        "predebit_notice_sent_at": NOON_IST - timedelta(hours=25),
    }
    defaults.update(overrides)
    return PolicyContext(**defaults)  # type: ignore[arg-type]


def retry_debit() -> ProposedAction:
    return ProposedAction(kind=ActionKind.RETRY_DEBIT)


def reminder(channel: Channel = Channel.SMS, **kw: object) -> ProposedAction:
    return ProposedAction(
        kind=ActionKind.SEND_REMINDER,
        channel=channel,
        template_id=kw.pop("template_id", "RP_DUNNING_01"),  # type: ignore[arg-type]
        variables=kw.pop(  # type: ignore[arg-type]
            "variables", {"merchant": "Acme", "amount": "499.00", "due_date": "2026-08-25"}
        ),
    )


# --- consent ---------------------------------------------------------------


def test_consent_refused_for_unconsented_channel() -> None:
    result = gate_consent(reminder(Channel.VOICE), make_ctx())
    assert not result.passed
    assert result.code is RefusalCode.NO_CONSENT


def test_consent_refused_on_purpose_mismatch() -> None:
    ctx = make_ctx(consented_purposes=frozenset({"marketing"}))
    result = gate_consent(reminder(), ctx)
    assert result.code is RefusalCode.CONSENT_PURPOSE_MISMATCH


def test_consent_ignores_actions_that_contact_nobody() -> None:
    assert gate_consent(retry_debit(), make_ctx()).passed


# --- suppression -----------------------------------------------------------


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"dispute_open": True}, RefusalCode.SUPPRESSED_DISPUTE),
        ({"do_not_contact": True}, RefusalCode.SUPPRESSED_DO_NOT_CONTACT),
        ({"subscription_active": False}, RefusalCode.SUBSCRIPTION_CANCELLED),
    ],
)
def test_suppression_blocks(override: dict[str, object], expected: RefusalCode) -> None:
    assert gate_suppression(reminder(), make_ctx(**override)).code is expected


def test_promise_to_pay_suppresses_until_its_date() -> None:
    until = NOON_IST + timedelta(days=3)
    result = gate_suppression(reminder(), make_ctx(promise_to_pay_until=until))
    assert result.code is RefusalCode.SUPPRESSED_PROMISE_TO_PAY
    assert result.retry_after == until


def test_expired_promise_to_pay_stops_suppressing() -> None:
    ctx = make_ctx(promise_to_pay_until=NOON_IST - timedelta(days=1))
    assert gate_suppression(reminder(), ctx).passed


# --- mandate: the RBI e-mandate sequence -----------------------------------


def test_debit_without_notice_is_refused_and_names_the_remedy() -> None:
    result = gate_mandate(retry_debit(), make_ctx(predebit_notice_sent_at=None))
    assert result.code is RefusalCode.PREDEBIT_NOTICE_REQUIRED
    assert result.remediation is ActionKind.SEND_PREDEBIT_NOTICE


def test_notice_must_mature_for_24h() -> None:
    sent = NOON_IST - timedelta(hours=6)
    result = gate_mandate(retry_debit(), make_ctx(predebit_notice_sent_at=sent))
    assert result.code is RefusalCode.PREDEBIT_NOTICE_NOT_MATURE
    assert result.retry_after == sent + timedelta(hours=24)


def test_matured_notice_permits_the_debit() -> None:
    assert gate_mandate(retry_debit(), make_ctx()).passed


def test_stale_notice_requires_a_fresh_one() -> None:
    ctx = make_ctx(predebit_notice_sent_at=NOON_IST - timedelta(days=9))
    result = gate_mandate(retry_debit(), ctx)
    assert result.code is RefusalCode.PREDEBIT_NOTICE_STALE
    assert result.remediation is ActionKind.SEND_PREDEBIT_NOTICE


def test_opt_out_is_terminal_not_a_detour() -> None:
    # The customer declined this debit. There is no remediation and no time at
    # which it becomes acceptable -- that is what makes it a stopping signal.
    result = gate_mandate(retry_debit(), make_ctx(predebit_opted_out=True))
    assert result.code is RefusalCode.PREDEBIT_OPTED_OUT
    assert result.remediation is None
    assert result.retry_after is None


def test_revoked_mandate_blocks_debit() -> None:
    assert (
        gate_mandate(retry_debit(), make_ctx(mandate_active=False)).code
        is RefusalCode.MANDATE_REVOKED
    )


# --- mandate: decline taxonomy --------------------------------------------


def test_hard_decline_blocks_retry_and_points_at_instrument_repair() -> None:
    ctx = make_ctx(case=make_case(decline_class=DeclineClass.HARD, reason="card_expired"))
    result = gate_mandate(retry_debit(), ctx)
    assert result.code is RefusalCode.HARD_DECLINE_NO_RETRY
    assert result.remediation is ActionKind.REQUEST_INSTRUMENT_UPDATE


def test_active_downtime_defers_rather_than_forbids() -> None:
    ends = NOON_IST + timedelta(hours=2)
    ctx = make_ctx(
        case=make_case(decline_class=DeclineClass.DOWNTIME, reason="bank_technical_error"),
        downtime_active=True,
        downtime_expected_end=ends,
    )
    result = gate_mandate(retry_debit(), ctx)
    assert result.code is RefusalCode.DOWNTIME_ACTIVE
    assert result.retry_after == ends


def test_resolved_downtime_permits_retry() -> None:
    ctx = make_ctx(
        case=make_case(decline_class=DeclineClass.DOWNTIME, reason="bank_technical_error"),
        downtime_active=False,
    )
    assert gate_mandate(retry_debit(), ctx).passed


def test_unclassified_decline_does_not_spend_an_attempt() -> None:
    ctx = make_ctx(case=make_case(decline_class=DeclineClass.UNKNOWN, reason="mystery_code"))
    assert gate_mandate(retry_debit(), ctx).code is RefusalCode.UNCLASSIFIED_DECLINE_NO_RETRY


# --- mandate: AFA thresholds ----------------------------------------------


def test_debit_within_afa_ceiling_is_permitted() -> None:
    ctx = make_ctx(case=make_case(amount=15_000_00))
    assert gate_mandate(retry_debit(), ctx).passed


def test_debit_above_afa_ceiling_needs_authentication() -> None:
    ctx = make_ctx(case=make_case(amount=15_000_01))
    result = gate_mandate(retry_debit(), ctx)
    assert result.code is RefusalCode.AFA_REQUIRED_ABOVE_THRESHOLD
    assert result.remediation is ActionKind.SEND_PAYMENT_LINK


@pytest.mark.parametrize("category", ["insurance", "mutual_fund", "credit_card_bill"])
def test_high_limit_categories_get_the_one_lakh_ceiling(category: str) -> None:
    ctx = make_ctx(case=make_case(amount=99_000_00), mandate_category=category)
    assert gate_mandate(retry_debit(), ctx).passed


def test_high_limit_category_still_capped_at_one_lakh() -> None:
    ctx = make_ctx(case=make_case(amount=1_00_000_01), mandate_category="insurance")
    assert gate_mandate(retry_debit(), ctx).code is RefusalCode.AFA_REQUIRED_ABOVE_THRESHOLD


def test_afa_carrying_attempt_bypasses_the_ceiling() -> None:
    ctx = make_ctx(case=make_case(amount=50_000_00), attempt_carries_afa=True)
    assert gate_mandate(retry_debit(), ctx).passed


# --- attempt budget --------------------------------------------------------


def test_internal_cap_binds_before_network_cap() -> None:
    ctx = make_ctx(case=make_case(attempts=4))
    assert gate_attempt_budget(retry_debit(), ctx).code is RefusalCode.INTERNAL_ATTEMPT_CAP


def test_network_ceiling_uses_the_stricter_default_when_unknown() -> None:
    ctx = make_ctx(attempts_last_30d=10, card_network=None)
    assert gate_attempt_budget(retry_debit(), ctx).code is RefusalCode.NETWORK_ATTEMPT_CAP


def test_visa_permits_more_attempts_than_the_default() -> None:
    ctx = make_ctx(attempts_last_30d=10, card_network="visa")
    assert gate_attempt_budget(retry_debit(), ctx).passed


def test_mastercard_ceiling_is_ten() -> None:
    ctx = make_ctx(attempts_last_30d=10, card_network="mastercard")
    assert gate_attempt_budget(retry_debit(), ctx).code is RefusalCode.NETWORK_ATTEMPT_CAP


def test_messages_do_not_consume_attempt_budget() -> None:
    ctx = make_ctx(case=make_case(attempts=99))
    assert gate_attempt_budget(reminder(), ctx).passed


# --- quiet hours -----------------------------------------------------------


@pytest.mark.parametrize(
    ("utc_hour", "expected_pass"),
    [
        (2, False),  # 07:30 IST, before the window opens
        (5, True),  # 10:30 IST
        (12, True),  # 17:30 IST
        (14, False),  # 19:30 IST, after it closes
        (20, False),  # 01:30 IST next day
    ],
)
def test_contact_window_follows_ist(utc_hour: int, expected_pass: bool) -> None:
    now = datetime(2026, 8, 23, utc_hour, 0, tzinfo=UTC)
    ctx = make_ctx(now=now, predebit_notice_sent_at=now - timedelta(hours=25))
    assert gate_quiet_hours(reminder(), ctx).passed is expected_pass


def test_quiet_hours_refusal_carries_the_next_open_time() -> None:
    now = datetime(2026, 8, 23, 20, 0, tzinfo=UTC)  # 01:30 IST
    result = gate_quiet_hours(reminder(), make_ctx(now=now))
    assert result.code is RefusalCode.OUTSIDE_CONTACT_HOURS
    assert result.retry_after is not None
    assert result.retry_after.astimezone(UTC) > now


def test_predebit_notice_is_exempt_from_contact_hours() -> None:
    # A statutory notice with a hard 24h lead cannot be deferred by an internal
    # comfort rule without costing the customer their opt-out window.
    now = datetime(2026, 8, 23, 20, 0, tzinfo=UTC)
    notice = ProposedAction(
        kind=ActionKind.SEND_PREDEBIT_NOTICE,
        channel=Channel.SMS,
        template_id="RP_PREDEBIT_01",
        variables={
            "merchant": "Acme",
            "amount": "499.00",
            "debit_at": "2026-08-25T10:00",
            "mandate_ref": "mnd_1",
            "reason": "subscription renewal",
        },
    )
    assert gate_quiet_hours(notice, make_ctx(now=now)).passed


# --- cooldown --------------------------------------------------------------


def test_cooldown_blocks_a_second_contact_too_soon() -> None:
    last = NOON_IST - timedelta(hours=6)
    result = gate_cooldown(reminder(), make_ctx(last_contact_at=last))
    assert result.code is RefusalCode.COOLDOWN_ACTIVE
    assert result.retry_after == last + timedelta(hours=48)


def test_cooldown_clears_after_the_gap() -> None:
    ctx = make_ctx(last_contact_at=NOON_IST - timedelta(hours=49))
    assert gate_cooldown(reminder(), ctx).passed


# --- template --------------------------------------------------------------


def test_registered_channel_requires_a_template() -> None:
    action = ProposedAction(kind=ActionKind.SEND_REMINDER, channel=Channel.SMS)
    assert gate_template(action, make_ctx()).code is RefusalCode.TEMPLATE_REQUIRED


def test_unregistered_template_is_refused() -> None:
    action = reminder(template_id="MADE_UP_TEMPLATE")
    assert gate_template(action, make_ctx()).code is RefusalCode.TEMPLATE_NOT_REGISTERED


def test_template_registered_for_another_channel_is_refused() -> None:
    action = reminder(Channel.WHATSAPP_UTILITY)  # RP_DUNNING_01 is SMS-only
    assert gate_template(action, make_ctx()).code is RefusalCode.TEMPLATE_NOT_REGISTERED


def test_missing_variable_is_refused() -> None:
    action = reminder(variables={"merchant": "Acme", "amount": "499.00"})
    result = gate_template(action, make_ctx())
    assert result.code is RefusalCode.TEMPLATE_VARIABLE_MISMATCH
    assert "due_date" in result.explanation


def test_extra_variable_is_refused() -> None:
    action = reminder(
        variables={
            "merchant": "Acme",
            "amount": "499.00",
            "due_date": "2026-08-25",
            "sneaky_freetext": "pay now or else",
        }
    )
    result = gate_template(action, make_ctx())
    assert result.code is RefusalCode.TEMPLATE_VARIABLE_MISMATCH
    assert "sneaky_freetext" in result.explanation


def test_email_does_not_require_registration() -> None:
    action = ProposedAction(kind=ActionKind.SEND_REMINDER, channel=Channel.EMAIL)
    assert gate_template(action, make_ctx()).passed


# --- channel economics -----------------------------------------------------


def test_voice_refused_when_the_invoice_cannot_justify_it() -> None:
    ctx = make_ctx(case=make_case(amount=500), consented_channels=frozenset({Channel.VOICE}))
    result = gate_channel_economics(reminder(Channel.VOICE), ctx)
    assert result.code is RefusalCode.NEGATIVE_EXPECTED_VALUE


def test_voice_permitted_on_a_large_invoice() -> None:
    ctx = make_ctx(case=make_case(amount=49900))
    assert gate_channel_economics(reminder(Channel.VOICE), ctx).passed


def test_expected_value_decays_with_each_attempt() -> None:
    # Same invoice, same channel: worth a voice call on the first attempt,
    # not worth one on the fourth. Decay is what makes escalation terminate.
    first = make_ctx(case=make_case(amount=1500, attempts=0))
    fourth = make_ctx(case=make_case(amount=1500, attempts=3))
    assert gate_channel_economics(reminder(Channel.VOICE), first).passed
    assert gate_channel_economics(reminder(Channel.VOICE), fourth).code is (
        RefusalCode.NEGATIVE_EXPECTED_VALUE
    )
    # A free channel is never blocked on economics, however decayed the case.
    assert gate_channel_economics(reminder(Channel.EMAIL), fourth).passed


# --- engine ----------------------------------------------------------------


def test_engine_runs_every_gate_without_short_circuiting() -> None:
    # Two independent violations; both must appear.
    ctx = make_ctx(
        predebit_notice_sent_at=None,
        case=make_case(attempts=4),
    )
    decision = PolicyEngine().evaluate(retry_debit(), ctx)
    assert len(decision.results) == 8
    assert RefusalCode.PREDEBIT_NOTICE_REQUIRED in decision.codes
    assert RefusalCode.INTERNAL_ATTEMPT_CAP in decision.codes


def test_permitted_decision_when_everything_clears() -> None:
    decision = PolicyEngine().evaluate(retry_debit(), make_ctx())
    assert decision.permitted
    assert decision.refusals == ()


def test_payload_records_passing_gates_too() -> None:
    # A compliance engine that only logs refusals cannot prove it ran.
    payload = PolicyEngine().evaluate(retry_debit(), make_ctx()).to_payload()
    gates = payload["gates"]
    assert isinstance(gates, list)
    assert len(gates) == 8
    assert all(g["passed"] for g in gates)  # type: ignore[index]


def test_refusal_is_recorded_to_the_ledger() -> None:
    ledger = Ledger(InMemoryLedger())
    ctx = make_ctx(predebit_notice_sent_at=None)
    PolicyEngine().evaluate_and_record(retry_debit(), ctx, ledger)
    history = ledger.history("case_1")
    assert len(history) == 1
    assert "predebit_notice_required" in history[0].summary


def test_first_permitted_returns_the_winner_and_every_refusal() -> None:
    engine = PolicyEngine()
    ctx = make_ctx(predebit_notice_sent_at=None)
    notice = ProposedAction(
        kind=ActionKind.SEND_PREDEBIT_NOTICE,
        channel=Channel.SMS,
        template_id="RP_PREDEBIT_01",
        variables={
            "merchant": "Acme",
            "amount": "499.00",
            "debit_at": "2026-08-25T10:00",
            "mandate_ref": "mnd_1",
            "reason": "subscription renewal",
        },
    )
    chosen, all_decisions = engine.first_permitted([retry_debit(), notice], ctx)
    assert chosen is not None
    assert chosen.action.kind is ActionKind.SEND_PREDEBIT_NOTICE
    assert len(all_decisions) == 2
    assert not all_decisions[0].permitted


def test_earliest_permissible_takes_the_latest_blocking_clock() -> None:
    sent = NOON_IST - timedelta(hours=6)
    last_contact = NOON_IST - timedelta(hours=1)
    ctx = make_ctx(predebit_notice_sent_at=sent, last_contact_at=last_contact)
    decision = PolicyEngine().evaluate(retry_debit(), ctx)
    # Only the mandate gate blocks a debit; cooldown ignores non-contact actions.
    assert decision.earliest_permissible_at == sent + timedelta(hours=24)


def test_opt_out_is_reported_as_permanently_blocked() -> None:
    decision = PolicyEngine().evaluate(retry_debit(), make_ctx(predebit_opted_out=True))
    assert decision.blocked_permanently
    assert remediation_plan(decision) == []


def test_notice_refusal_yields_a_usable_remediation_plan() -> None:
    decision = PolicyEngine().evaluate(retry_debit(), make_ctx(predebit_notice_sent_at=None))
    assert remediation_plan(decision) == [ActionKind.SEND_PREDEBIT_NOTICE]
