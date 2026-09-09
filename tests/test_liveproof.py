"""Live execution-proof tests, against a real Razorpay response.

`tests/fixtures/razorpay_recovered_link.json` was captured from the live test
API: one order carrying a real failed card payment and a real captured
netbanking payment. Testing against it means the parsing is checked against
what Razorpay actually returns rather than what we assumed it would.

The property these defend hardest is separation. This proof says the execution
boundary is real; it says nothing about recovery lift. R1's Rs 1,08,422 comes
from a controlled simulated experiment, and the two must never be presentable
as one number.
"""

from __future__ import annotations

import json
from pathlib import Path

from recovery.domain.failure import DeclineClass
from recovery.live.liveproof import ExecutionProof, read_proof

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "razorpay_recovered_link.json").read_text()
)
PROOF = read_proof(FIXTURE["link"], FIXTURE["payments"]["items"])


# --- what really happened ---------------------------------------------------


def test_it_reads_the_real_failure() -> None:
    assert PROOF.failure is not None
    assert PROOF.failure.payment_id == "pay_TZnf3gmB3xYMPt"
    assert PROOF.failure.status == "failed"
    assert PROOF.failure.error_reason == "international_transaction_not_allowed"


def test_it_reads_the_real_recovery() -> None:
    assert PROOF.recovery is not None
    assert PROOF.recovery.payment_id == "pay_TZnhafQGqOSMU6"
    assert PROOF.recovery.status == "captured"
    assert PROOF.recovery.amount_paise == 9900
    assert PROOF.recovery.method == "netbanking"


def test_the_failure_and_the_recovery_are_the_same_order() -> None:
    # This is the attribution. Without a shared order id these are two
    # unrelated payments and the loop has not been shown to close.
    assert PROOF.failure is not None and PROOF.recovery is not None
    assert PROOF.failure.order_id == PROOF.recovery.order_id == "order_TZnd73R2UVEcRE"
    assert PROOF.attributed is True


def test_the_link_moved_from_created_to_paid() -> None:
    assert PROOF.link_status == "paid"
    assert PROOF.amount_paid_paise == 9900


# --- what our own taxonomy makes of a code it has never seen ---------------


def test_an_unseen_provider_code_is_classified_unknown_not_guessed() -> None:
    """The design decision in architecture.md section 3, on real data.

    An unrecognised reason means the provider changed something or we are
    seeing an uncharacterised instrument. Letting it inherit retry permission
    would spend an attempt on a failure mode we cannot name -- and here that
    would mean retrying an international card against a domestic-only account,
    which fails every time.
    """
    assert PROOF.decline_class is DeclineClass.UNKNOWN
    assert PROOF.retry_permitted is False


def test_the_policy_engine_refuses_a_retry_on_it() -> None:
    assert PROOF.policy_refusal is not None
    assert "unclassified_decline_no_retry" in PROOF.policy_refusal


def test_the_refusal_names_a_remedy() -> None:
    assert PROOF.policy_remediation == "escalate"


# --- separation from the experiment ----------------------------------------


def test_the_proof_reports_only_what_razorpay_confirms() -> None:
    # Every figure here is a real payment amount, not a modelled one.
    assert PROOF.recovered_amount == "Rs 99.00"
    assert PROOF.payload()["kind"] == "execution_proof"


def test_the_proof_carries_no_experimental_figure() -> None:
    """R1's number must never travel with a real payment id.

    The two claims answer different questions -- does the strategy work, and
    can the system operate against payment infrastructure -- and merging them
    would turn a controlled simulated result into an implied cash figure.
    """
    body = json.dumps(PROOF.payload())

    for experimental in ("108421", "1,08,421", "0.2353", "23.5"):
        assert experimental not in body


def test_the_payload_serialises() -> None:
    json.dumps(PROOF.payload())


# --- honest absence ---------------------------------------------------------


def test_an_unpaid_link_reports_no_recovery_rather_than_zero() -> None:
    link = dict(FIXTURE["link"], status="created", amount_paid=0, payments=[])
    failure_only = [p for p in FIXTURE["payments"]["items"] if p["status"] == "failed"]

    proof = read_proof(link, failure_only)

    assert proof.recovery is None
    assert proof.attributed is False
    assert proof.recovered_amount == "Rs 0.00"


def test_a_link_with_nothing_against_it_is_reported_as_such() -> None:
    proof = read_proof(dict(FIXTURE["link"], status="created", amount_paid=0, payments=[]), [])

    assert proof.failure is None
    assert proof.recovery is None
    assert isinstance(proof, ExecutionProof)
