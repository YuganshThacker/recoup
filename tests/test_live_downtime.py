"""Live downtime panel tests.

One property matters more than the rest: **the panel never invents an outage.**
When credentials are missing, the SDK is not installed, or the API call fails,
it reports that plainly and shows nothing. A console that filled the gap with
plausible bank codes would be turning the most checkable thing on screen into
the least trustworthy.
"""

from __future__ import annotations

from typing import Any

from recovery.live.downtime import DowntimeSource, Outage, read_outages


class _Feed:
    """Stands in for DowntimeFeed with whatever the API would have returned."""

    def __init__(self, items: list[Any] | None = None, raises: Exception | None = None) -> None:
        self._items = items or []
        self._raises = raises
        self.calls = 0

    def current(self) -> list[Any]:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self._items


class _Downtime:
    def __init__(
        self,
        method: str,
        severity: str = "high",
        status: str = "started",
        instrument: dict[str, str] | None = None,
        scheduled: bool = False,
    ) -> None:
        self.method = method
        self.severity = severity
        self.status = status
        self.scheduled = scheduled
        self.instrument = instrument or {}

    @property
    def blocking(self) -> bool:
        return self.status == "started" and self.severity == "high"


# --- reading the feed ------------------------------------------------------


def test_it_reads_the_instrument_code_and_what_kind_it_is() -> None:
    # Razorpay names the affected thing differently per method: issuer for
    # cards, bank for netbanking, vpa_handle for UPI.
    outages = read_outages(
        [
            _Downtime("card", instrument={"issuer": "CITI"}),
            _Downtime("netbanking", instrument={"bank": "DLXB"}),
            _Downtime("upi", instrument={"vpa_handle": "kotak811"}),
        ]
    )

    assert [(o.method, o.kind, o.instrument) for o in outages] == [
        ("card", "issuer", "CITI"),
        ("netbanking", "bank", "DLXB"),
        ("upi", "vpa_handle", "kotak811"),
    ]


def test_an_outage_with_no_named_instrument_is_still_reported() -> None:
    # A method-wide degradation names nothing. Dropping it would hide the
    # broadest outage there is.
    outages = read_outages([_Downtime("upi", instrument={})])

    assert outages[0].instrument == "all"


def test_non_blocking_outages_are_marked_rather_than_dropped() -> None:
    # A scheduled or low-severity entry is real and worth showing; it just does
    # not gate anything.
    outages = read_outages([_Downtime("card", severity="low", instrument={"issuer": "HDFC"})])

    assert outages[0].blocking is False


# --- the source ------------------------------------------------------------


def test_a_source_with_no_feed_reports_why_and_shows_nothing() -> None:
    view = DowntimeSource(feed=None, reason="RAZORPAY_KEY_ID is not set").view()

    assert view.available is False
    assert view.outages == ()
    assert "RAZORPAY_KEY_ID" in (view.reason or "")


def test_a_failing_call_reports_the_failure_rather_than_an_empty_all_clear() -> None:
    # "No outages" and "we could not ask" look identical on a dashboard and mean
    # opposite things.
    view = DowntimeSource(feed=_Feed(raises=RuntimeError("connection reset"))).view()

    assert view.available is False
    assert view.outages == ()
    assert "connection reset" in (view.reason or "")


def test_a_working_feed_reports_available_with_a_summary() -> None:
    feed = _Feed(
        [
            _Downtime("card", instrument={"issuer": "CITI"}),
            _Downtime("card", instrument={"issuer": "PUNB"}),
            _Downtime("upi", instrument={"vpa_handle": "kotak811"}),
        ]
    )

    view = DowntimeSource(feed=feed).view()

    assert view.available is True
    assert view.reason is None
    assert len(view.outages) == 3
    assert view.summary == {"card": 2, "upi": 1}
    assert view.fetched_at is not None


def test_an_empty_working_feed_is_available_and_says_so() -> None:
    # Genuinely no outages is a different state from cannot ask, and the panel
    # has to be able to express it.
    view = DowntimeSource(feed=_Feed([])).view()

    assert view.available is True
    assert view.outages == ()


def test_the_summary_counts_only_blocking_outages() -> None:
    feed = _Feed(
        [
            _Downtime("card", instrument={"issuer": "CITI"}),
            _Downtime("card", severity="low", instrument={"issuer": "HDFC"}),
        ]
    )

    view = DowntimeSource(feed=feed).view()

    assert view.summary == {"card": 1}
    assert len(view.outages) == 2, "the non-blocking one is still listed"


def test_the_view_is_json_safe() -> None:
    import json

    view = DowntimeSource(feed=_Feed([_Downtime("card", instrument={"issuer": "CITI"})])).view()

    json.dumps(view.payload())


def test_from_env_without_credentials_reports_that_rather_than_raising() -> None:
    # The console must start with no credentials configured at all.
    source = DowntimeSource.from_env(environ={})

    assert source.view().available is False
    assert "RAZORPAY_KEY_ID" in (source.view().reason or "")


def test_outages_carry_a_stable_label_for_the_panel() -> None:
    assert (
        Outage(
            method="card",
            kind="issuer",
            instrument="CITI",
            severity="high",
            status="started",
            scheduled=False,
            blocking=True,
        ).label
        == "CITI"
    )
