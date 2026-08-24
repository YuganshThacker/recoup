"""Money as integer minor units (paise). No floats, anywhere, ever.

Razorpay represents INR amounts as integer paise: ``amount: 50000`` is Rs 500.00.
We keep that representation end to end -- database, JSON, aggregation -- and only
format to rupees at the display boundary.

Float money is banned here for two independent reasons:

1. Accumulation error across a batch of thousands of recovery cases silently
   corrupts the headline number the whole project is judged on.
2. Razorpay webhook signature verification hashes the *raw* body. Parsing to
   float and reserialising changes the bytes and produces intermittent
   signature mismatches that are miserable to debug.
"""

from __future__ import annotations

from decimal import Decimal
from typing import NewType

Paise = NewType("Paise", int)
"""Indian rupees in integer minor units. 100 paise == 1 rupee."""

PAISE_PER_RUPEE = 100


def paise(value: int) -> Paise:
    """Construct a Paise value, rejecting non-integers and negatives."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"paise() requires int minor units, got {type(value).__name__}: {value!r}")
    if value < 0:
        raise ValueError(f"paise() requires a non-negative amount, got {value}")
    return Paise(value)


def from_rupees(value: Decimal | int | str) -> Paise:
    """Convert a rupee amount to paise.

    Accepts Decimal, int, or str. Rejects float outright -- if a float reaches
    this function, a money value has already been through a lossy
    representation somewhere upstream and the fix belongs there, not here.
    """
    if isinstance(value, float):
        raise TypeError(
            "from_rupees() refuses float. Use Decimal('123.45') or a string; "
            "a float here means precision was already lost upstream."
        )
    rupees = Decimal(value)
    minor = rupees * PAISE_PER_RUPEE
    if minor != minor.to_integral_value():
        raise ValueError(f"{rupees} rupees is not a whole number of paise")
    return paise(int(minor))


def to_rupees(amount: Paise) -> Decimal:
    """Exact rupee value for display and reporting."""
    return Decimal(int(amount)) / PAISE_PER_RUPEE


def format_inr(amount: Paise) -> str:
    """Render for humans: ``format_inr(paise(1234500))`` -> ``'Rs 12,345.00'``.

    Uses plain ``Rs`` rather than the rupee sign so the output survives
    terminals, log aggregators, and CSV exports without encoding surprises.
    """
    rupees = to_rupees(amount)
    whole, _, frac = f"{rupees:.2f}".partition(".")
    # Indian digit grouping: last three digits, then pairs (12,34,567).
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups: list[str] = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join([*groups, tail])
    return f"Rs {whole}.{frac}"


def total(amounts: list[Paise]) -> Paise:
    """Sum paise values. Integer arithmetic, so exact by construction."""
    return paise(sum(int(a) for a in amounts))


def format_signed_inr(minor_units: int) -> str:
    """Format a signed paise quantity for display.

    :func:`paise` rejects negatives on purpose -- an amount is never negative.
    A *difference* between amounts legitimately is, and a negative contribution
    is a real result that must be reportable rather than crash the report.
    """
    sign = "-" if minor_units < 0 else ""
    return f"{sign}{format_inr(paise(abs(minor_units)))}"
