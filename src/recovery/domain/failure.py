"""Decline taxonomy: the analytical core of the recovery system.

Razorpay attaches four fields to every payment error -- ``code``, ``source``,
``step``, ``reason`` -- which answer who / where / why. We branch on ``reason``
(machine-readable) and never on ``description`` (human prose, subject to change).

Every reason maps to exactly one :class:`DeclineClass`, and the class determines
what recovery is even *permitted*:

``HARD``
    The instrument cannot succeed. Retrying is pure waste, and on card networks
    it is a rules violation with a literal per-attempt price (Visa's "Decline
    Category 1 -- never retry"; excessive-reattempt fees beyond 15/30 days).
    The only valid move is instrument repair.

``SOFT``
    Retry is legitimate. The entire question is *when* -- which is where the
    lift in this project actually comes from.

``DOWNTIME``
    Not the customer's problem. Retrying during an issuer or gateway outage
    burns an attempt against the 30-day budget and adds load to a system that
    is already failing. Gate on the ``payment.downtime.resolved`` signal rather
    than on a timer.

``UNKNOWN``
    Unmapped reason string. Deliberately *not* folded into SOFT: an unmapped
    code is a signal that Razorpay changed something or that we are looking at
    an instrument we have not characterised. These route to the agent tail for
    diagnosis rather than silently inheriting retry permission.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DeclineClass(StrEnum):
    """What kind of failure this is, in terms of what recovery is possible."""

    HARD = "hard"
    SOFT = "soft"
    DOWNTIME = "downtime"
    UNKNOWN = "unknown"

    @property
    def allows_debit_retry(self) -> bool:
        """May we attempt another debit on the same instrument?

        UNKNOWN returns False: we do not spend a customer's goodwill or an
        attempt-budget slot on a failure mode we cannot name.
        """
        return self is DeclineClass.SOFT

    @property
    def requires_instrument_repair(self) -> bool:
        """Is a new instrument or mandate the only path to recovery?"""
        return self is DeclineClass.HARD

    @property
    def blocked_pending_downtime(self) -> bool:
        """Must we wait for an external resolve signal before acting?"""
        return self is DeclineClass.DOWNTIME


class PaymentMethod(StrEnum):
    CARD = "card"
    UPI = "upi"
    NETBANKING = "netbanking"
    WALLET = "wallet"
    EMANDATE = "emandate"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class DeclineReason:
    """A characterised Razorpay failure reason."""

    reason: str
    decline_class: DeclineClass
    method: PaymentMethod
    summary: str


# Source: Razorpay error-code documentation for cards and UPI.
# Verified against docs 2026-08-23. Unmapped reasons fall through to UNKNOWN by
# design -- see module docstring.
_CARD_REASONS: tuple[tuple[str, DeclineClass, str], ...] = (
    ("insufficient_funds", DeclineClass.SOFT, "Account lacked funds at attempt time"),
    ("payment_timed_out", DeclineClass.SOFT, "Customer exceeded the ~10 minute window"),
    ("payment_cancelled", DeclineClass.SOFT, "Customer cancelled or navigated back"),
    ("authentication_failed", DeclineClass.SOFT, "Wrong OTP or authentication abandoned"),
    ("incorrect_cvv", DeclineClass.SOFT, "CVV mismatch"),
    ("card_declined", DeclineClass.SOFT, "Issuer declined without a specific reason"),
    ("payment_failed", DeclineClass.SOFT, "Issuer declined"),
    ("payment_risk_check_failed", DeclineClass.SOFT, "Issuer flagged as potentially fraudulent"),
    ("transaction_limit_exceeded", DeclineClass.SOFT, "Daily transaction cap reached"),
    ("bank_downtime", DeclineClass.DOWNTIME, "Issuer unavailable"),
    ("bank_technical_error", DeclineClass.DOWNTIME, "Issuer technical failure"),
    ("gateway_technical_error", DeclineClass.DOWNTIME, "Gateway or partner bank failure"),
    ("card_expired", DeclineClass.HARD, "Card past expiry"),
    ("card_not_enrolled", DeclineClass.HARD, "Card not enabled for online transactions"),
    ("card_disabled_for_online_payments", DeclineClass.HARD, "Online payments disabled by holder"),
    ("debit_instrument_inactive", DeclineClass.HARD, "Instrument never activated"),
    ("debit_instrument_blocked", DeclineClass.HARD, "Instrument blocked by holder or issuer"),
)

_UPI_REASONS: tuple[tuple[str, DeclineClass, str], ...] = (
    ("insufficient_funds", DeclineClass.SOFT, "Account lacked funds at attempt time"),
    ("payment_collect_request_expired", DeclineClass.SOFT, "Collect request TTL elapsed"),
    ("payment_timed_out", DeclineClass.SOFT, "Customer exceeded the processing window"),
    ("payment_cancelled", DeclineClass.SOFT, "Customer declined in their PSP app"),
    ("payment_declined", DeclineClass.SOFT, "Debit could not be completed"),
    ("bank_technical_error", DeclineClass.DOWNTIME, "UPI provider downtime"),
    ("gateway_technical_error", DeclineClass.DOWNTIME, "Gateway failure"),
    ("credit_failed", DeclineClass.DOWNTIME, "Beneficiary-side credit failure"),
    ("invalid_vpa", DeclineClass.HARD, "Not a valid UPI user"),
    ("vpa_resolution_failed", DeclineClass.HARD, "UPI handle could not be resolved"),
    (
        "customer_bank_account_mismatch",
        DeclineClass.HARD,
        "Different account than the one registered on the mandate",
    ),
)


def _build_registry() -> dict[tuple[PaymentMethod, str], DeclineReason]:
    registry: dict[tuple[PaymentMethod, str], DeclineReason] = {}
    # e-mandate recurring debits surface card-style reasons, so they share the table.
    for method, rows in (
        (PaymentMethod.CARD, _CARD_REASONS),
        (PaymentMethod.EMANDATE, _CARD_REASONS),
        (PaymentMethod.UPI, _UPI_REASONS),
    ):
        for reason, decline_class, summary in rows:
            registry[(method, reason)] = DeclineReason(reason, decline_class, method, summary)
    return registry


_REGISTRY = _build_registry()


def classify(reason: str | None, method: PaymentMethod = PaymentMethod.UNKNOWN) -> DeclineReason:
    """Map a Razorpay ``error_reason`` to its decline class.

    Unmapped reasons -- including ``None``, which happens when a charge fails
    without an error payload -- return UNKNOWN rather than guessing. Callers
    route UNKNOWN to the agent tail; see :mod:`recovery.policy`.
    """
    if reason:
        known = _REGISTRY.get((method, reason))
        if known is not None:
            return known
        # Same reason string on an uncharacterised method: trust the string,
        # since Razorpay reuses reasons across methods consistently.
        for (_, candidate_reason), entry in _REGISTRY.items():
            if candidate_reason == reason:
                return DeclineReason(reason, entry.decline_class, method, entry.summary)
    return DeclineReason(
        reason=reason or "unspecified",
        decline_class=DeclineClass.UNKNOWN,
        method=method,
        summary="Unmapped failure reason; routed for diagnosis",
    )


def known_reasons(method: PaymentMethod) -> frozenset[str]:
    """Reason strings characterised for a method. Used by the batch generator."""
    return frozenset(reason for (m, reason) in _REGISTRY if m is method)
