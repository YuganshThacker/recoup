"""Hidden ground truth for simulated cases.

Each simulated case carries facts the recovery system cannot see: when funds
actually return to the account, when an outage actually ends, whether the
customer would replace a dead card if asked, and whether they were going to pay
of their own accord anyway. A debit succeeds or fails as a consequence of those
facts and the *time it is attempted* -- never as a consequence of who attempted
it or which experiment arm they belong to.

That last property is what makes the comparison meaningful. Neither arm gets a
better world; they get the same world and differ only in when and whether they
act.

WHAT THIS CAN AND CANNOT SHOW
-----------------------------
This world model encodes a hypothesis: that recovery is largely a matter of
attempting at the right moment, and that decline codes carry information about
when that moment is. **A simulation built on that hypothesis cannot be used to
confirm it.** If retry timing does not matter in reality, these runs would not
reveal it.

What the batch does establish:

* the policy correctly exploits the structure it is given -- it times retries
  after funds return, waits out downtime rather than burning attempts into it,
  and does not attempt debits that cannot succeed;
* the full machinery runs end to end at batch scale -- detection, gating,
  execution, idempotency, stopping rules, ledger, measurement;
* the measurement apparatus itself works, including the holdout and the
  self-cure baseline.

Confirming the hypothesis needs real merchant data. That is a limitation of the
evidence, stated plainly, not a defect to be papered over.

All probabilities and interval choices below are ESTIMATES. They are published
in the generator config so a reader can disagree with them specifically.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from recovery.domain.failure import DeclineClass, classify
from recovery.domain.money import Paise, paise

# Subscription price points, in paise, with rough real-world weighting.
_PRICE_POINTS: tuple[tuple[int, int], ...] = (
    (99_00, 18),
    (199_00, 22),
    (299_00, 16),
    (499_00, 18),
    (999_00, 12),
    (1_999_00, 8),
    (4_999_00, 4),
    (14_999_00, 2),
)

# Batch A decline mix, as published in docs/analysis-plan.md.
_DECLINE_MIX: tuple[tuple[str, int], ...] = (
    ("insufficient_funds", 40),
    ("card_declined", 6),
    ("payment_failed", 4),
    ("authentication_failed", 10),
    ("transaction_limit_exceeded", 5),
    ("payment_risk_check_failed", 3),
    ("bank_technical_error", 9),
    ("gateway_technical_error", 6),
    ("card_expired", 8),
    ("debit_instrument_blocked", 4),
    ("debit_instrument_inactive", 3),
    ("unmapped_provider_code", 2),
)

# ESTIMATE. Probability the customer pays of their own accord, with no
# intervention at all. This is the baseline both arms share, and the reason a
# holdout exists: without it, every post-intervention payment would be
# miscredited to the agent.
SELF_CURE_RATE = 0.22

# ESTIMATE. Probability a customer replaces the instrument when asked. The only
# route out of a hard decline.
INSTRUMENT_REPAIR_RATE = 0.31

# ESTIMATE. Share of generic issuer declines that are permanently lost.
GENERIC_DECLINE_LOST_RATE = 0.30

# ESTIMATE. Probability the customer sends a free-text reply, which is what
# routes a case to the inbound-freetext tail.
INBOUND_REPLY_RATE = 0.12


@dataclass(frozen=True, slots=True)
class GroundTruth:
    """What is actually true about a case. Never visible to the recovery system."""

    recoverable_from: datetime | None
    """Earliest moment a debit would succeed. ``None`` means no debit ever will,
    unless the instrument is repaired."""

    self_cure_at: datetime | None
    """When the customer would pay unprompted, if they would at all."""

    downtime_ends_at: datetime | None
    repairable: bool
    """Whether an instrument-update request would be honoured."""

    sends_inbound_reply: bool


def _weighted_choice(rng: random.Random, options: tuple[tuple[object, int], ...]) -> object:
    total = sum(weight for _, weight in options)
    draw = rng.uniform(0, total)
    upto = 0.0
    for value, weight in options:
        upto += weight
        if draw <= upto:
            return value
    return options[-1][0]


def _next_cash_in(rng: random.Random, after: datetime) -> datetime:
    """Next plausible moment funds return to a salaried account.

    Indian payroll clusters at month end and the first few days of the month.
    This is the structure a timing-aware policy can exploit and a fixed
    T+1/T+2/T+3 schedule cannot -- which is the whole mechanism under test.
    """
    candidate = after + timedelta(days=1)
    for _ in range(40):
        day = candidate.day
        if day <= 3 or day >= 28:
            jitter = timedelta(hours=rng.randint(0, 23), minutes=rng.randint(0, 59))
            return candidate.replace(hour=0, minute=0, second=0, microsecond=0) + jitter
        candidate += timedelta(days=1)
    return after + timedelta(days=7)


def _recoverable_from(
    rng: random.Random, reason: str, decline_class: DeclineClass, failed_at: datetime
) -> tuple[datetime | None, datetime | None]:
    """When a debit would start succeeding, and when downtime ends."""
    if decline_class is DeclineClass.HARD:
        return None, None

    if decline_class is DeclineClass.DOWNTIME:
        ends = failed_at + timedelta(minutes=rng.randint(30, 12 * 60))
        return ends, ends

    if reason == "insufficient_funds":
        return _next_cash_in(rng, failed_at), None

    if reason == "transaction_limit_exceeded":
        return failed_at + timedelta(days=1, hours=rng.randint(0, 12)), None

    if reason in {"authentication_failed", "payment_timed_out", "payment_cancelled"}:
        # Transient and customer-side: clears quickly.
        return failed_at + timedelta(hours=rng.randint(1, 48)), None

    if decline_class is DeclineClass.UNKNOWN:
        if rng.random() < 0.5:
            return None, None
        return failed_at + timedelta(days=rng.randint(1, 6)), None

    # Generic issuer declines: some clear, some never do.
    if rng.random() < GENERIC_DECLINE_LOST_RATE:
        return None, None
    return failed_at + timedelta(days=rng.randint(1, 9)), None


def make_ground_truth(
    rng: random.Random, reason: str, failed_at: datetime, window: timedelta
) -> GroundTruth:
    """Draw the hidden facts for one case."""
    decline_class = classify(reason).decline_class
    recoverable_from, downtime_ends_at = _recoverable_from(rng, reason, decline_class, failed_at)

    self_cure_at: datetime | None = None
    if rng.random() < SELF_CURE_RATE:
        offset = timedelta(seconds=rng.randint(0, int(window.total_seconds())))
        self_cure_at = failed_at + offset

    return GroundTruth(
        recoverable_from=recoverable_from,
        self_cure_at=self_cure_at,
        downtime_ends_at=downtime_ends_at,
        repairable=rng.random() < INSTRUMENT_REPAIR_RATE,
        sends_inbound_reply=rng.random() < INBOUND_REPLY_RATE,
    )


def draw_amount(rng: random.Random) -> Paise:
    """A subscription price point."""
    return paise(int(_weighted_choice(rng, _PRICE_POINTS)))  # type: ignore[call-overload]


def draw_decline_reason(rng: random.Random) -> str:
    """A failure reason from the published Batch A mix."""
    return str(_weighted_choice(rng, _DECLINE_MIX))
