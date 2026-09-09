"""The execution proof: one real payment, verified against Razorpay.

This answers a different question from R1, R2 and R3, and the separation is the
whole point of it existing.

    experimental proof   does the recovery strategy work?
                         R1/R2/R3, controlled, simulated, reproducible

    execution proof      can the system operate against real payment
                         infrastructure?
                         this module, real Razorpay state, n = 1

**The two must never be presentable as one number.** R1's Rs 1,08,422 is a
modelled figure from a controlled experiment and is labelled as such
everywhere; a real ``payment_id`` must never travel alongside it, because that
would turn a simulated result into an implied cash claim. There is a test
asserting no experimental figure appears in this payload.

What is real here, end to end on one Razorpay order:

* a **failed** payment, with the provider's own error code
* our decline taxonomy classifying that code, having never seen it before
* the policy engine refusing a retry on it, with a named remedy
* a **captured** payment on the same order
* the amount, confirmed by Razorpay rather than asserted by us

The failure that produced this was an accident: an international test card
against a domestic-only account. That is why it is worth showing. The code
``international_transaction_not_allowed`` is not in our registry, and the
system did the conservative thing with it rather than guessing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from recovery.domain.failure import DeclineClass, PaymentMethod, classify
from recovery.domain.money import format_inr, paise
from recovery.policy.actions import ActionKind, Channel, ProposedAction
from recovery.policy.decision import Decision
from recovery.policy.engine import PolicyEngine
from recovery.policy.gates import PolicyContext
from recovery.templates import REGISTERED

PROOF_KIND = "execution_proof"


@dataclass(frozen=True, slots=True)
class RealPayment:
    """One payment as Razorpay reports it."""

    payment_id: str
    status: str
    amount_paise: int
    method: str
    order_id: str | None
    at: str
    error_reason: str | None = None
    error_description: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "payment_id": self.payment_id,
            "status": self.status,
            "amount": format_inr(paise(self.amount_paise)),
            "method": self.method,
            "order_id": self.order_id,
            "at": self.at,
            "error_reason": self.error_reason,
            "error_description": self.error_description,
        }


@dataclass(frozen=True, slots=True)
class ExecutionProof:
    """What Razorpay confirms, and what our own policy made of it."""

    link_id: str
    link_status: str
    link_url: str
    reference_id: str | None
    order_id: str | None
    amount_paise: int
    amount_paid_paise: int
    failure: RealPayment | None
    recovery: RealPayment | None
    decline_class: DeclineClass | None
    retry_permitted: bool
    policy_refusal: str | None
    policy_remediation: str | None

    @property
    def attributed(self) -> bool:
        """Same order, so the recovery answers the failure.

        Without a shared order id these are two unrelated payments and the loop
        has not been shown to close.
        """
        return (
            self.failure is not None
            and self.recovery is not None
            and self.failure.order_id is not None
            and self.failure.order_id == self.recovery.order_id
        )

    @property
    def recovered_amount(self) -> str:
        return format_inr(paise(self.recovery.amount_paise if self.recovery else 0))

    def payload(self) -> dict[str, Any]:
        return {
            "kind": PROOF_KIND,
            "link_id": self.link_id,
            "link_status": self.link_status,
            "link_url": self.link_url,
            "reference_id": self.reference_id,
            "order_id": self.order_id,
            "amount": format_inr(paise(self.amount_paise)),
            "amount_paid": format_inr(paise(self.amount_paid_paise)),
            "failure": self.failure.payload() if self.failure else None,
            "recovery": self.recovery.payload() if self.recovery else None,
            "decline_class": self.decline_class.value if self.decline_class else None,
            "retry_permitted": self.retry_permitted,
            "policy_refusal": self.policy_refusal,
            "policy_remediation": self.policy_remediation,
            "attributed": self.attributed,
            "recovered_amount": self.recovered_amount,
        }


def _payment(raw: dict[str, Any]) -> RealPayment:
    created = raw.get("created_at")
    at = (
        datetime.fromtimestamp(int(created), tz=UTC).isoformat(timespec="seconds")
        if created
        else ""
    )
    return RealPayment(
        payment_id=str(raw.get("id", "")),
        status=str(raw.get("status", "")),
        amount_paise=int(raw.get("amount") or 0),
        method=str(raw.get("method", "")),
        order_id=raw.get("order_id"),
        at=at,
        error_reason=raw.get("error_reason"),
        error_description=raw.get("error_description"),
    )


def read_proof(link: dict[str, Any], payments: list[dict[str, Any]]) -> ExecutionProof:
    """Build the proof from a Razorpay payment link and its payments.

    A pure function of the provider's own response, so it is tested against a
    captured real one rather than against an assumption about its shape.
    """
    order_id = link.get("order_id")
    on_order = [p for p in payments if not order_id or p.get("order_id") == order_id]

    failed = [_payment(p) for p in on_order if p.get("status") == "failed"]
    captured = [_payment(p) for p in on_order if p.get("status") in ("captured", "authorized")]
    failure = min(failed, key=lambda p: p.at) if failed else None
    recovery = max(captured, key=lambda p: p.at) if captured else None

    decline = _classify(failure)
    refusal, remediation = _policy(failure, link) if failure else (None, None)

    return ExecutionProof(
        link_id=str(link.get("id", "")),
        link_status=str(link.get("status", "")),
        link_url=str(link.get("short_url", "")),
        reference_id=link.get("reference_id"),
        order_id=order_id,
        amount_paise=int(link.get("amount") or 0),
        amount_paid_paise=int(link.get("amount_paid") or 0),
        failure=failure,
        recovery=recovery,
        decline_class=decline,
        retry_permitted=bool(decline and decline.allows_debit_retry),
        policy_refusal=refusal,
        policy_remediation=remediation,
    )


def _classify(failure: RealPayment | None) -> DeclineClass | None:
    if failure is None:
        return None
    try:
        method = PaymentMethod(failure.method)
    except ValueError:
        method = PaymentMethod.UNKNOWN
    return classify(failure.error_reason, method).decline_class


def _policy(failure: RealPayment, link: dict[str, Any]) -> tuple[str | None, str | None]:
    """Run the real engine on the real failure.

    Not a description of what the policy would do -- the same
    :class:`~recovery.policy.engine.PolicyEngine` the batches use, given the
    provider's own error code.
    """
    from recovery.domain.case import RecoveryCase

    reason = failure.error_reason
    case = RecoveryCase(
        case_id=str(link.get("reference_id") or link.get("id")),
        subscription_id="n/a",
        invoice_id=str(link.get("order_id") or ""),
        customer_id="n/a",
        amount=paise(int(link.get("amount") or 0)),
        method=PaymentMethod.CARD,
        decline_reason=reason,
        decline_class=_classify(failure) or DeclineClass.UNKNOWN,
        detected_at=datetime.now(UTC),
    )
    ctx = PolicyContext(
        case=case,
        now=datetime.now(UTC),
        consented_channels=frozenset({Channel.SMS}),
        consented_purposes=frozenset(t.purpose for t in REGISTERED.values()),
        predebit_notice_sent_at=datetime.now(UTC),
        templates=dict(REGISTERED),
    )
    decision: Decision = PolicyEngine().evaluate(ProposedAction(kind=ActionKind.RETRY_DEBIT), ctx)
    refusals = [r for r in decision.results if not r.passed]
    if not refusals:
        return None, None
    first = refusals[0]
    codes = ", ".join(f"{r.gate.value}={r.code.value}" for r in refusals if r.code)
    return codes, first.remediation.value if first.remediation else None


# --- fetching it live -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProofView:
    """The proof, or a stated reason there isn't one.

    Same discipline as the downtime panel: "we could not ask" and "nothing was
    recovered" look identical on a screen and mean opposite things, so
    availability is carried separately from the result.
    """

    available: bool
    reason: str | None
    proof: ExecutionProof | None

    def payload(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "reason": self.reason,
            "proof": self.proof.payload() if self.proof else None,
        }


def find_proof(gateway: Any, *, link_id: str | None = None) -> ProofView:
    """The paid link on this account, and what the system makes of it.

    Discovered rather than pinned: any link that has actually been paid will
    do, so this keeps working if a different one is used later.
    """
    try:
        if link_id:
            link = gateway.get(f"/payment_links/{link_id}")
        else:
            listing = gateway.get("/payment_links", count=100)
            links = listing.get("payment_links") or listing.get("items") or []
            paid = [i for i in links if i.get("status") == "paid"]
            if not paid:
                return ProofView(
                    available=False,
                    reason="no payment link on this account has been paid yet",
                    proof=None,
                )
            link = gateway.get(f"/payment_links/{paid[0]['id']}")

        payments = gateway.get("/payments", count=100).get("items", [])
    except Exception as exc:  # a provider failure is a panel state, not a crash
        return ProofView(available=False, reason=f"{type(exc).__name__}: {exc}", proof=None)

    return ProofView(available=True, reason=None, proof=read_proof(link, payments))


def proof_from_env(*, link_id: str | None = None) -> ProofView:
    """Build from credentials, reporting rather than raising when absent."""
    import os

    if not os.environ.get("RAZORPAY_KEY_ID") or not os.environ.get("RAZORPAY_KEY_SECRET"):
        return ProofView(
            available=False,
            reason="RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET are not set",
            proof=None,
        )
    try:
        from recovery.providers.razorpay import RazorpayGateway

        gateway = RazorpayGateway.from_env()
    except ImportError:
        return ProofView(
            available=False,
            reason="the razorpay extra is not installed (pip install -e '.[razorpay]')",
            proof=None,
        )
    except Exception as exc:
        return ProofView(available=False, reason=f"{type(exc).__name__}: {exc}", proof=None)

    try:
        return find_proof(gateway, link_id=link_id)
    finally:
        gateway.close()
