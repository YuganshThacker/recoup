"""Regulatory and economic constants, each with its source.

Every value here is a number the system's behaviour depends on and that a
reviewer might challenge. They live in one file, with citations, so the answer
to "where did 15,000 come from" is one grep rather than an archaeology exercise.

Values marked ESTIMATE are our own operating assumptions, not regulation. They
are separated deliberately: a wrong estimate is a tuning problem, a wrong
regulatory constant is a compliance incident.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from recovery.domain.money import Paise, paise

# --- RBI: Digital Payments E-mandate Framework, 2026 -----------------------
# Recurring debits may skip Additional Factor Authentication up to this amount.
AFA_EXEMPT_LIMIT: Paise = paise(15_000_00)

# Raised ceiling for specific categories: insurance premiums, mutual fund
# subscriptions, credit card bill payments.
AFA_EXEMPT_LIMIT_HIGH: Paise = paise(1_00_000_00)
AFA_HIGH_LIMIT_CATEGORIES: frozenset[str] = frozenset(
    {"insurance", "mutual_fund", "credit_card_bill"}
)

# A pre-debit notification must reach the customer at least this far ahead of
# each debit, carrying merchant name, amount, debit date/time, mandate
# reference and reason, with a per-debit opt-out.
PREDEBIT_NOTICE_LEAD: timedelta = timedelta(hours=24)

# ESTIMATE. Not regulation: our own staleness bound. A notice quoting a debit
# date long past no longer describes what we are about to do, so we re-notify
# rather than lean on it.
PREDEBIT_NOTICE_MAX_AGE: timedelta = timedelta(days=7)

# --- RBI: Fair Practices Code (recovery conduct) ---------------------------
# Contact with a customer about money owed is confined to these hours, local
# time. Applies to calls, SMS, WhatsApp and email alike.
CONTACT_WINDOW_OPEN_HOUR = 8
CONTACT_WINDOW_CLOSE_HOUR = 19
CUSTOMER_TIMEZONE = ZoneInfo("Asia/Kolkata")

# --- Card network reattempt limits ----------------------------------------
# Ceilings on reattempts of a single declined transaction within 30 days.
# Exceeding them carries per-attempt fees, so these are costs, not etiquette.
NETWORK_30D_ATTEMPT_LIMITS: dict[str, int] = {
    "visa": 15,
    "mastercard": 10,
}
# Applied when the network is unknown. Deliberately the stricter of the two.
DEFAULT_30D_ATTEMPT_LIMIT = 10

# ESTIMATE. Our own per-case cap, far below any network ceiling. This is the
# limit that actually binds in practice; the network numbers are the legal
# ceiling we must not approach.
INTERNAL_MAX_ATTEMPTS_PER_CASE = 4

# --- Channel unit economics ------------------------------------------------
# ESTIMATE. WhatsApp figures track published India per-message rates; SMS and
# voice are order-of-magnitude operating assumptions. Exact values matter less
# than the ratios, which are what drive escalation decisions.
CHANNEL_COST: dict[str, Paise] = {
    "email": paise(0),
    "whatsapp_utility": paise(16),
    "sms": paise(20),
    "whatsapp_marketing": paise(88),
    "voice": paise(400),
}

# ESTIMATE. Prior probability that a case recovers given one more action of the
# right kind, by decline class. Seeded from published dunning benchmarks and
# replaced by measured rates once the first batch has run.
BASE_RECOVERY_PRIOR: dict[str, Decimal] = {
    "soft": Decimal("0.48"),
    "downtime": Decimal("0.62"),
    "hard": Decimal("0.09"),
    "unknown": Decimal("0.25"),
}

# ESTIMATE. Each successive attempt on the same case recovers less.
ATTEMPT_DECAY = Decimal("0.62")

# --- Contact frequency -----------------------------------------------------
# ESTIMATE. Minimum gap between two outbound contacts on the same case.
CONTACT_COOLDOWN: timedelta = timedelta(hours=48)
