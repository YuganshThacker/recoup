"""Inbound understanding tests.

The model reads customer messages into facts the policy engine acts on, so the
properties asserted here are the ones that stop a misreading becoming a wrong
money action: the contract holds, a bad reading is rejected rather than used,
and the corpus the benchmark scores against is internally consistent.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from recovery.agent.client import ModelReply, ScriptedClient, UnavailableClient
from recovery.agent.inbound import (
    InboundExtractor,
    InboundIntent,
    InvalidReading,
    keyword_reading,
    reading_schema,
    validate_reading,
)
from recovery.sim.inbound_corpus import CATEGORIES, CORPUS, TODAY

NOW = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)

VALID = {
    "intent": "promise_to_pay",
    "promised_date": "2026-08-28",
    "requests_no_retry": False,
    "confidence": 0.9,
    "verbatim": "I'll pay Friday",
}


# --- the contract ----------------------------------------------------------


def test_schema_has_no_amount_field() -> None:
    # Same guarantee as the action schema. A figure a customer claims is not a
    # figure the ledger accepts, so there is nowhere for one to travel.
    properties = reading_schema()["properties"]
    assert "amount" not in properties
    assert not set(properties) & {"amount_paise", "amount_due", "value"}


def test_schema_is_strict_mode_compatible() -> None:
    schema = reading_schema()
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False


def test_unclear_is_available_so_the_model_can_decline() -> None:
    # Forcing a choice on ambiguous text produces confident wrong answers, and
    # the policy engine acts on what comes back.
    assert "unclear" in reading_schema()["properties"]["intent"]["enum"]


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ({"intent": "demand_money"}, "not a known intent"),
        ({"promised_date": "next friday"}, "not an ISO date"),
        ({"promised_date": 42}, "ISO date string or null"),
        ({"confidence": 1.4}, "out of range"),
        ({"confidence": "high"}, "must be a number"),
        ({"verbatim": None}, "must be a string"),
    ],
)
def test_validate_rejects_malformed_readings(mutation: dict[str, object], match: str) -> None:
    with pytest.raises(InvalidReading, match=match):
        validate_reading({**VALID, **mutation})


def test_an_injected_amount_is_ignored() -> None:
    reading = validate_reading({**VALID, "amount": 999_00, "waive_fee": True})
    assert not hasattr(reading, "amount")
    assert reading.intent is InboundIntent.PROMISE_TO_PAY


# --- facts, not actions ----------------------------------------------------


def test_promise_becomes_a_suppression_window() -> None:
    reading = validate_reading(VALID)
    facts = reading.policy_facts(now=NOW)
    assert facts["promise_to_pay_until"].date() == date(2026, 8, 28)
    assert "do_not_contact" not in facts


def test_dispute_opens_a_dispute_not_an_action() -> None:
    facts = validate_reading({**VALID, "intent": "dispute_already_paid"}).policy_facts(now=NOW)
    assert facts["dispute_open"] is True
    # The gate decides what happens next; the reading only states what is true.
    assert not any(k.startswith("action") for k in facts)


def test_explicit_stop_request_sets_do_not_contact() -> None:
    facts = validate_reading(
        {**VALID, "intent": "request_stop_retries", "requests_no_retry": True}
    ).policy_facts(now=NOW)
    assert facts["do_not_contact"] is True


def test_multi_intent_keeps_both_facts() -> None:
    # The documented convention: a stop request that also names a date keeps
    # the date, so the engine has everything the customer said.
    facts = validate_reading(
        {**VALID, "intent": "request_stop_retries", "requests_no_retry": True}
    ).policy_facts(now=NOW)
    assert facts["do_not_contact"] is True
    assert facts["promise_to_pay_until"].date() == date(2026, 8, 28)


def test_a_question_produces_no_policy_facts() -> None:
    facts = validate_reading(
        {**VALID, "intent": "general_question", "promised_date": None}
    ).policy_facts(now=NOW)
    assert facts == {}


# --- the baseline ----------------------------------------------------------


def test_baseline_reads_plain_messages() -> None:
    for text, expected in (
        ("Please stop retrying my card", InboundIntent.REQUEST_STOP_RETRIES),
        ("I already paid this invoice", InboundIntent.DISPUTE_ALREADY_PAID),
        ("I will pay on 2026-08-28", InboundIntent.PROMISE_TO_PAY),
        ("Can I use a different card", InboundIntent.PAYMENT_METHOD_CHANGE),
    ):
        assert keyword_reading(text, today=TODAY).intent is expected


def test_baseline_resolves_relative_dates() -> None:
    assert keyword_reading("I'll pay tomorrow", today=TODAY).promised_date == date(2026, 8, 26)
    assert keyword_reading("I'll pay in 3 days", today=TODAY).promised_date == date(2026, 8, 28)


def test_baseline_needs_a_verb_to_see_a_promise() -> None:
    # A real limitation, pinned rather than tuned away: a bare relative
    # expression carries no promise keyword, so the baseline declines. Widening
    # the term list until this passes would be fitting the baseline to the
    # corpus it is about to be scored against.
    assert keyword_reading("in 3 days", today=TODAY).intent is InboundIntent.UNCLEAR


def test_baseline_is_competent_enough_to_be_a_fair_opponent() -> None:
    # A comparison against a hobbled baseline proves nothing. This pins the
    # baseline's floor so it cannot silently rot into a strawman.
    correct = sum(keyword_reading(m.text, today=TODAY).intent is m.intent for m in CORPUS)
    assert correct / len(CORPUS) > 0.70


# --- extraction, and its floor --------------------------------------------


def test_model_reading_is_used_when_valid() -> None:
    extractor = InboundExtractor(ScriptedClient([VALID]))
    reading, used_model = extractor.read("I'll pay Friday", today=TODAY)
    assert used_model
    assert reading.promised_date == date(2026, 8, 28)
    assert extractor.fallbacks == 0


def test_model_outage_falls_back_to_keywords() -> None:
    extractor = InboundExtractor(UnavailableClient())
    reading, used_model = extractor.read("please stop retrying", today=TODAY)
    assert not used_model
    assert reading.intent is InboundIntent.REQUEST_STOP_RETRIES
    assert extractor.fallbacks == 1


def test_malformed_reading_falls_back_rather_than_being_used() -> None:
    extractor = InboundExtractor(ScriptedClient([{**VALID, "intent": "seize_assets"}]))
    reading, used_model = extractor.read("I already paid", today=TODAY)
    assert not used_model
    assert reading.intent is InboundIntent.DISPUTE_ALREADY_PAID
    assert extractor.fallbacks == 1
    assert any("invalid_reading" in e for e in extractor.errors)


def test_extraction_is_free_when_the_call_fails() -> None:
    reply = ModelReply.failure("boom", model="m")
    assert reply.total_tokens == 0


# --- corpus integrity ------------------------------------------------------


def test_corpus_labels_are_internally_consistent() -> None:
    # A label that contradicts itself would quietly score both approaches wrong.
    for m in CORPUS:
        if m.requests_no_retry:
            assert m.intent is InboundIntent.REQUEST_STOP_RETRIES, m.text
        if m.intent is InboundIntent.UNCLEAR:
            assert m.promised_date is None, m.text
        assert m.category in CATEGORIES, m.text


def test_corpus_is_not_stacked_toward_adversarial_cases() -> None:
    # Most of the corpus should be ordinary messages either approach can read.
    # If the model only wins on the hard slice, the aggregate must show it.
    ordinary = sum(1 for m in CORPUS if m.category in ("plain", "relative", "hinglish"))
    assert ordinary / len(CORPUS) > 0.55


def test_corpus_covers_every_intent() -> None:
    assert {m.intent for m in CORPUS} == set(InboundIntent)


def test_corpus_is_large_enough_to_resolve_the_effect() -> None:
    # At n=47 the same comparison came back p=0.23. Size is what made it
    # answerable, and shrinking the corpus should break this test.
    assert len(CORPUS) >= 150
