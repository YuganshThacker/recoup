"""Razorpay adapter.

What this account can actually reach, verified against the live test API rather
than assumed from the docs:

    payments, orders, customers, payment_links, invoices, items,
    refunds, settlements, addons, payments/downtimes    -> available
    plans, subscriptions, tokens, virtual_accounts       -> 401 / 404

Subscriptions is a separately-enabled product and is **not** on this account,
which matters because the project's vertical is subscription dunning. The
consequence is stated plainly rather than worked around: a real mandate debit
cannot be performed here, so :meth:`RazorpayGateway.charge` refuses instead of
pretending. The recurring-charge path is measured against the simulator, and
the real API carries the parts of the workflow it genuinely supports --
recovery payment links, invoices, and the live downtime feed.

The downtime feed is the most useful of those. The policy engine already
refuses debits during an outage; wiring it to Razorpay's published downtimes
means that refusal is driven by real issuer state rather than a flag we made
up.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

from recovery.domain.money import Paise
from recovery.providers.base import MessageReceipt

API_ROOT = "https://api.razorpay.com/v1"
DEFAULT_TIMEOUT_SECONDS = 20.0

# Razorpay rate-limits object creation, and test mode is tighter than live.
# A 429 is not an error condition -- it is the provider asking us to slow
# down -- so it is retried with exponential backoff rather than surfaced.
RETRYABLE_STATUS: frozenset[int] = frozenset({429, 500, 502, 503, 504})
MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 1.0

# Razorpay marks an outage high when the issuer, bank or network is down;
# medium on elevated declines or a low success rate; low when the cause is
# unknown and impact is minimal. We treat high and medium as blocking, because
# spending a capped attempt into either is waste.
BLOCKING_SEVERITIES: frozenset[str] = frozenset({"high", "medium"})
ACTIVE_STATUSES: frozenset[str] = frozenset({"started", "updated"})


class RazorpayError(Exception):
    """A Razorpay call failed in a way the caller must handle."""


@dataclass(frozen=True, slots=True)
class Downtime:
    """One published payment downtime."""

    id: str
    method: str
    status: str
    severity: str
    scheduled: bool
    instrument: dict[str, str]

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def blocking(self) -> bool:
        return self.active and self.severity in BLOCKING_SEVERITIES

    @property
    def keys(self) -> set[str]:
        """Identifiers this downtime covers, for matching against an instrument.

        Razorpay names the affected thing differently per method -- ``bank`` for
        netbanking, ``issuer`` for cards, ``vpa_handle`` for UPI, ``network``
        for a card network -- so all of them are indexed.
        """
        return {str(v).upper() for v in self.instrument.values() if v}


@dataclass
class DowntimeFeed:
    """Live view of Razorpay's published outages.

    Cached with a short TTL: the batch consults this per case, and a
    per-decision HTTP call would make the runner's latency a function of the
    provider's.
    """

    gateway: RazorpayGateway
    ttl_seconds: float = 120.0
    _items: tuple[Downtime, ...] = ()
    _fetched_at: float = 0.0

    def refresh(self) -> tuple[Downtime, ...]:
        body = self.gateway.get("/payments/downtimes")
        self._items = tuple(
            Downtime(
                id=str(i.get("id", "")),
                method=str(i.get("method", "")),
                status=str(i.get("status", "")),
                severity=str(i.get("severity", "")),
                scheduled=bool(i.get("scheduled", False)),
                instrument=dict(i.get("instrument") or {}),
            )
            for i in body.get("items", [])
        )
        self._fetched_at = time.monotonic()
        return self._items

    def current(self) -> tuple[Downtime, ...]:
        if not self._fetched_at or time.monotonic() - self._fetched_at > self.ttl_seconds:
            return self.refresh()
        return self._items

    def is_down(self, *, method: str, instrument: str | None = None) -> bool:
        """Is this payment route currently degraded?

        With no instrument, answers whether the method is degraded at all. With
        one, matches the issuer, bank, VPA handle or network Razorpay named.
        """
        needle = (instrument or "").upper()
        for d in self.current():
            if not d.blocking or d.method != method:
                continue
            if not needle or needle in d.keys:
                return True
        return False

    def summary(self) -> dict[str, int]:
        """Blocking outages per method, for the report."""
        counts: dict[str, int] = {}
        for d in self.current():
            if d.blocking:
                counts[d.method] = counts.get(d.method, 0) + 1
        return counts


@dataclass
class RazorpayGateway:
    """Authenticated Razorpay client, scoped to what this account can do."""

    key_id: str
    key_secret: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    calls: int = 0
    throttled: int = 0
    _client: Any | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls, **kwargs: Any) -> RazorpayGateway:
        """Build from RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET."""
        key_id = os.environ.get("RAZORPAY_KEY_ID", "")
        secret = os.environ.get("RAZORPAY_KEY_SECRET", "")
        if not key_id or not secret:
            raise RazorpayError(
                "RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET must be set; put them in .env"
            )
        return cls(key_id=key_id, key_secret=secret, **kwargs)

    @property
    def is_test_mode(self) -> bool:
        """Test keys carry an ``rzp_test_`` prefix.

        Checked before anything is created, so a live key cannot quietly be
        used to make real objects during a demo run.
        """
        return self.key_id.startswith("rzp_test_")

    def _http(self) -> Any:
        if self._client is None:
            import httpx2 as httpx

            self._client = httpx.Client(
                base_url=API_ROOT,
                auth=(self.key_id, self.key_secret),
                timeout=self.timeout_seconds,
            )
        return self._client

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        return self._request("GET", path, params=params or None)

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", path, json=body)

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """Issue one request, retrying only what is safe to retry.

        Retries cover 429 and 5xx. Both are cases where the request either did
        not take effect or the provider explicitly asked us to back off; a 4xx
        that is not 429 means the request was wrong and repeating it would only
        be wrong again.
        """
        last = ""
        for attempt in range(MAX_RETRIES):
            self.calls += 1
            response = self._http().request(method, path, **kwargs)
            if response.status_code < 400:
                data: dict[str, Any] = response.json()
                return data

            last = f"{response.status_code}: {response.text[:200]}"
            if response.status_code not in RETRYABLE_STATUS:
                break
            if attempt == MAX_RETRIES - 1:
                break
            # Honour Retry-After when the provider sends one; it knows better
            # than our backoff curve does.
            header = response.headers.get("retry-after")
            delay = (
                float(header)
                if header and header.isdigit()
                else (BACKOFF_BASE_SECONDS * (2**attempt))
            )
            self.throttled += 1
            time.sleep(delay)

        raise RazorpayError(f"{method} {path} -> {last}")

    # --- what this account supports ---------------------------------------

    def create_customer(self, *, name: str, email: str, contact: str) -> dict[str, Any]:
        return self.post(
            "/customers",
            {"name": name, "email": email, "contact": contact, "fail_existing": "0"},
        )

    def create_order(
        self, *, amount: Paise, receipt: str, notes: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """An order in integer paise, exactly as Razorpay expects it."""
        return self.post(
            "/orders",
            {
                "amount": int(amount),
                "currency": "INR",
                "receipt": receipt,
                "notes": notes or {},
            },
        )

    def create_recovery_link(
        self,
        *,
        amount: Paise,
        reference_id: str,
        description: str,
        expire_by: int | None = None,
        notes: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """A payment link, which is a real recovery action this account can take.

        ``reference_id`` carries our case id, so an inbound webhook can be
        matched back to the case that caused the link without a lookup table.
        """
        body: dict[str, Any] = {
            "amount": int(amount),
            "currency": "INR",
            "description": description,
            "reference_id": reference_id,
            "notify": {"sms": False, "email": False},
            "reminder_enable": False,
            "notes": notes or {},
        }
        if expire_by:
            body["expire_by"] = expire_by
        return self.post("/payment_links", body)

    def fetch_payment_link(self, link_id: str) -> dict[str, Any]:
        return self.get(f"/payment_links/{link_id}")

    def charge(self, **_: Any) -> None:
        """Not available on this account.

        A mandate debit needs the Subscriptions product, which returns 401 here.
        Refusing is the honest behaviour: silently substituting a payment link
        would report a customer-authenticated payment as an automatic debit and
        make the recovery numbers mean something they do not.
        """
        raise RazorpayError(
            "mandate debit needs the Subscriptions API, which is not enabled on "
            "this account (/v1/subscriptions returns 401). The recurring-charge "
            "path is measured against the simulator; see docs/analysis-plan.md"
        )

    def send_message(self, **_: Any) -> MessageReceipt:
        """Not wired. Outbound messaging needs DLT-registered templates."""
        raise RazorpayError(
            "outbound messaging is not wired to a live channel; templates must be "
            "DLT-registered before anything can be sent to a real customer"
        )

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


__all__ = [
    "BLOCKING_SEVERITIES",
    "Downtime",
    "DowntimeFeed",
    "RazorpayError",
    "RazorpayGateway",
]
