"""Razorpay's published payment downtime, as the console shows it.

The panel exists because this is the one thing on screen that is unarguably
live: real bank codes, fetched from the API while the audience watches, feeding
a gate that already consumes them. ``gate_mandate`` refuses a retry into an
issuer that is currently degraded, and this is where that signal comes from.

**It never invents an outage.** Missing credentials, an uninstalled optional
SDK, and a failed call all produce the same visible result -- nothing listed --
paired with the reason. That distinction is load-bearing: on a dashboard,
"no outages" and "we could not ask" look identical and mean opposite things, so
the view reports ``available`` separately from the list.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

# Razorpay names the affected thing differently per method. Ordered by how
# specific the identifier is, so a downtime carrying several is labelled by the
# narrowest one.
_INSTRUMENT_KEYS: tuple[str, ...] = ("issuer", "bank", "vpa_handle", "psp", "network")


@dataclass(frozen=True, slots=True)
class Outage:
    """One published downtime, flattened for display."""

    method: str
    kind: str
    """Which field named the instrument: issuer, bank, vpa_handle, network."""

    instrument: str
    severity: str
    status: str
    scheduled: bool
    blocking: bool
    """Whether this is severe enough to gate a retry."""

    @property
    def label(self) -> str:
        return self.instrument


@dataclass(frozen=True, slots=True)
class DowntimeView:
    """What the panel renders, including why it may be empty."""

    available: bool
    reason: str | None
    outages: tuple[Outage, ...]
    summary: dict[str, int]
    fetched_at: str | None

    def payload(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "reason": self.reason,
            "fetched_at": self.fetched_at,
            "summary": self.summary,
            "outages": [
                {
                    "method": o.method,
                    "kind": o.kind,
                    "instrument": o.instrument,
                    "severity": o.severity,
                    "status": o.status,
                    "scheduled": o.scheduled,
                    "blocking": o.blocking,
                }
                for o in self.outages
            ],
        }


class Feed(Protocol):
    """What this module needs from ``DowntimeFeed``. Kept narrow so the panel
    is testable without a network or an SDK."""

    def current(self) -> Any: ...


def read_outages(items: Any) -> tuple[Outage, ...]:
    """Flatten provider downtimes for display."""
    found = []
    for item in items:
        instrument = dict(getattr(item, "instrument", {}) or {})
        kind, code = _identify(instrument)
        found.append(
            Outage(
                method=str(getattr(item, "method", "")),
                kind=kind,
                instrument=code,
                severity=str(getattr(item, "severity", "")),
                status=str(getattr(item, "status", "")),
                scheduled=bool(getattr(item, "scheduled", False)),
                blocking=bool(getattr(item, "blocking", False)),
            )
        )
    return tuple(found)


def _identify(instrument: dict[str, str]) -> tuple[str, str]:
    """Which field named the affected thing, and what it said.

    A downtime with no named instrument is a method-wide degradation -- the
    broadest kind there is -- so it is labelled ``all`` rather than dropped.
    """
    for key in _INSTRUMENT_KEYS:
        value = instrument.get(key)
        if value:
            return key, str(value)
    for key, value in instrument.items():
        if value:
            return key, str(value)
    return "method", "all"


@dataclass
class DowntimeSource:
    """The feed, or a stated reason there isn't one."""

    feed: Feed | None = None
    reason: str | None = None

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> DowntimeSource:
        """Build from credentials, reporting rather than raising when absent.

        The console has to start with nothing configured. A missing key is an
        ordinary state for this panel, not an error for the server.
        """
        env = environ if environ is not None else dict(os.environ)
        if not env.get("RAZORPAY_KEY_ID") or not env.get("RAZORPAY_KEY_SECRET"):
            return cls(reason="RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET are not set")
        try:
            from recovery.providers.razorpay import DowntimeFeed, RazorpayGateway

            gateway = RazorpayGateway(
                key_id=env["RAZORPAY_KEY_ID"], key_secret=env["RAZORPAY_KEY_SECRET"]
            )
            return cls(feed=DowntimeFeed(gateway=gateway))
        except ImportError:
            return cls(reason="the razorpay extra is not installed (pip install -e '.[razorpay]')")
        except Exception as exc:  # credentials present but unusable
            return cls(reason=f"{type(exc).__name__}: {exc}")

    def view(self) -> DowntimeView:
        """Fetch, or explain why not. Never raises into the console."""
        if self.feed is None:
            return _unavailable(self.reason or "no downtime feed configured")
        try:
            items = self.feed.current()
        except Exception as exc:  # a provider failure is a panel state, not a crash
            return _unavailable(f"{type(exc).__name__}: {exc}")

        outages = read_outages(items)
        summary: dict[str, int] = {}
        for outage in outages:
            if outage.blocking:
                summary[outage.method] = summary.get(outage.method, 0) + 1

        return DowntimeView(
            available=True,
            reason=None,
            outages=outages,
            summary=summary,
            fetched_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )


def _unavailable(reason: str) -> DowntimeView:
    return DowntimeView(available=False, reason=reason, outages=(), summary={}, fetched_at=None)
