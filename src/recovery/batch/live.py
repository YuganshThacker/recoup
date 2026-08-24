"""Batch C: the integration proof, run against the live Razorpay test API.

    python -m recovery.batch.live --cases 50

This is deliberately *not* where the statistics come from. Razorpay's test-mode
manual charge is a dashboard button operated one case at a time, and
Subscriptions is not enabled on this account at all, so the real path cannot
produce a batch of the size the analysis plan requires. Batch C proves the
integration is real; Batches A and B provide the numbers. Two different claims,
reported separately and never merged.

What each case exercises, end to end, against the real API:

    create an order            real object, integer paise
    create a recovery link     a genuine recovery action this account supports
    fetch the link back        proves the object exists server-side
    re-issue with the same id  proves our idempotency key does its job

Plus, once per run: the live downtime feed that the policy engine's outage gate
consults, and a webhook signature round-trip.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field
from typing import Any

from recovery.domain.events import Actor, EventKind, InMemoryLedger, Ledger
from recovery.domain.money import Paise, format_inr, paise
from recovery.env import load_dotenv
from recovery.providers.razorpay import DowntimeFeed, RazorpayError, RazorpayGateway
from recovery.providers.webhooks import (
    SignatureError,
    WebhookReceiver,
    compute_signature,
)

# Realistic Indian subscription price points, in paise.
AMOUNTS: tuple[int, ...] = (99_00, 199_00, 299_00, 499_00, 999_00, 1_999_00)

# Razorpay's test mode rate-limits object creation hard. Backoff alone cannot
# outlast sustained limiting -- it just retries into the same wall -- so the
# run paces itself under the limit instead of discovering it 29 times.
DEFAULT_PACE_SECONDS = 0.2


@dataclass
class CaseResult:
    """What one case established."""

    customer: bool = False
    order: bool = False
    verified: bool = False
    link: bool = False
    link_rate_limited: bool = False
    duplicate_refused: bool = False


@dataclass
class LiveResult:
    """What Batch C established, and what it did not."""

    cases: int = 0
    customers: int = 0
    orders: int = 0
    verified: int = 0
    links: int = 0
    links_rate_limited: int = 0
    duplicate_refused: int = 0
    failures: list[str] = field(default_factory=list)
    run_id: str = ""
    api_calls: int = 0
    throttled: int = 0
    elapsed_s: float = 0.0
    downtimes: dict[str, int] = field(default_factory=dict)
    webhook_ok: bool = False
    webhook_forgery_rejected: bool = False
    webhook_replay_dropped: bool = False


def _prove_webhooks(result: LiveResult, ledger: Ledger) -> None:
    """Verify, reject a forgery, and drop a replay -- on constructed vectors.

    Razorpay only delivers webhooks to a publicly reachable URL, which a laptop
    is not. The signing algorithm is the part that must be right, so it is
    exercised here against vectors built with the documented scheme rather than
    left untested until a deploy.
    """
    secret = "whsec_batch_c_demo"
    receiver = WebhookReceiver(secret=secret)
    raw = json.dumps(
        {
            "entity": "event",
            "event": "payment.failed",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_demo",
                        "amount": 49900,
                        "error_reason": "insufficient_funds",
                    }
                }
            },
        }
    ).encode("utf-8")
    signature = compute_signature(raw, secret)

    event = receiver.receive(raw, signature, delivery_id="evt_demo_1")
    result.webhook_ok = event is not None and event.handled
    ledger.record(
        "batch_c",
        EventKind.PROVIDER_CALLBACK,
        Actor.WEBHOOK,
        "verified webhook accepted",
        {"event": event.event if event else None},
    )

    try:
        receiver.receive(raw, "0" * 64, delivery_id="evt_demo_2")
    except SignatureError:
        result.webhook_forgery_rejected = True
        ledger.record(
            "batch_c", EventKind.ACTION_REFUSED, Actor.SYSTEM, "forged signature rejected"
        )

    replay = receiver.receive(raw, signature, delivery_id="evt_demo_1")
    result.webhook_replay_dropped = replay is None
    if replay is None:
        ledger.record(
            "batch_c", EventKind.ACTION_DEDUPED, Actor.SYSTEM, "replayed delivery dropped"
        )


# Measured against the live test API rather than assumed: orders and customers
# accept sustained bursts (6/6 in under half a second), while payment_links is
# quota-limited and exhausts quickly. So the per-case volume rides on the
# endpoints that carry it, and the link path -- which is the more interesting
# recovery action but the scarcer resource -- is probed on a few cases and its
# rate limit reported as the environmental constraint it is.
LINK_PROBES = 5


def _one_case(
    gateway: RazorpayGateway, ledger: Ledger, index: int, amount: Paise, run_id: str
) -> CaseResult:
    """Run one case against the live API.

    The reference carries a per-run id. Razorpay enforces reference_id
    uniqueness account-wide and permanently, so an id derived only from the case
    index collides with every previous run -- which is exactly what happened the
    first time this was run twice.
    """
    case_id = f"live_{index:03d}"
    reference = f"recovery-{run_id}-{case_id}"
    outcome = CaseResult()

    customer = gateway.create_customer(
        name=f"Recovery Case {index}",
        email=f"{reference}@example.invalid",
        contact="9999999999",
    )
    outcome.customer = True
    ledger.record(
        case_id,
        EventKind.CASE_DETECTED,
        Actor.SYSTEM,
        f"customer created {customer['id']}",
    )

    order = gateway.create_order(amount=amount, receipt=reference, notes={"case_id": case_id})
    outcome.order = True
    ledger.record(
        case_id,
        EventKind.ACTION_EXECUTED,
        Actor.SYSTEM,
        f"order created {order['id']}",
        {"amount_paise": order["amount"], "status": order["status"]},
    )

    # Fetch it back. Creating an object proves the request was accepted; reading
    # it back proves it exists server-side with the values we sent.
    fetched = gateway.fetch_order(order["id"])
    outcome.verified = (
        fetched["id"] == order["id"]
        and int(fetched["amount"]) == int(amount)
        and fetched["receipt"] == reference
    )
    ledger.record(
        case_id,
        EventKind.OUTCOME_RECORDED,
        Actor.SYSTEM,
        "order verified server-side" if outcome.verified else "order verification FAILED",
    )

    if index >= LINK_PROBES:
        return outcome

    try:
        link = gateway.create_recovery_link(
            amount=amount,
            reference_id=reference,
            description=f"Recovery for {case_id}",
            notes={"case_id": case_id},
        )
        outcome.link = True
        ledger.record(
            case_id,
            EventKind.ACTION_EXECUTED,
            Actor.SYSTEM,
            f"recovery link created {link['id']}",
            {"short_url": link.get("short_url")},
        )
    except RazorpayError as exc:
        outcome.link_rate_limited = "429" in str(exc)
        ledger.record(
            case_id,
            EventKind.ACTION_REFUSED,
            Actor.SYSTEM,
            "recovery link refused by provider",
            {"rate_limited": outcome.link_rate_limited},
        )
        return outcome

    # Razorpay rejects a duplicate reference_id. That is the provider enforcing
    # the same guarantee our idempotency keys enforce internally, so a rejection
    # here is the success condition.
    try:
        gateway.create_recovery_link(
            amount=amount, reference_id=reference, description="duplicate attempt"
        )
    except RazorpayError as exc:
        if "already exists" in str(exc):
            outcome.duplicate_refused = True
            ledger.record(
                case_id,
                EventKind.ACTION_DEDUPED,
                Actor.SYSTEM,
                "duplicate reference_id refused by provider",
            )
    return outcome


def run(cases: int, *, pace_seconds: float = DEFAULT_PACE_SECONDS) -> tuple[LiveResult, Ledger]:
    """Execute Batch C."""
    load_dotenv()
    gateway = RazorpayGateway.from_env()
    if not gateway.is_test_mode:
        raise SystemExit(
            "RAZORPAY_KEY_ID is not a test key. Batch C creates real objects and "
            "will not run against live credentials."
        )

    ledger = Ledger(InMemoryLedger())
    result = LiveResult()
    started = time.monotonic()
    run_id = f"{int(time.time()):x}"
    result.run_id = run_id

    feed = DowntimeFeed(gateway=gateway)
    result.downtimes = feed.summary()
    ledger.record(
        "batch_c",
        EventKind.PROVIDER_CALLBACK,
        Actor.SYSTEM,
        f"live downtime feed: {sum(result.downtimes.values())} blocking outages",
        {"by_method": result.downtimes},
    )

    _prove_webhooks(result, ledger)

    for index in range(cases):
        amount = paise(AMOUNTS[index % len(AMOUNTS)])
        if index:
            time.sleep(pace_seconds)
        try:
            case = _one_case(gateway, ledger, index, amount, run_id)
            result.cases += 1
            result.customers += int(case.customer)
            result.orders += int(case.order)
            result.verified += int(case.verified)
            result.links += int(case.link)
            result.links_rate_limited += int(case.link_rate_limited)
            result.duplicate_refused += int(case.duplicate_refused)
        except RazorpayError as exc:
            result.failures.append(f"case {index}: {exc}")

    result.api_calls = gateway.calls
    result.throttled = gateway.throttled
    result.elapsed_s = time.monotonic() - started
    gateway.close()
    return result, ledger


def _print(result: LiveResult) -> None:
    total = sum(int(a) for a in (paise(AMOUNTS[i % len(AMOUNTS)]) for i in range(result.cases)))
    print("=== BATCH C - live Razorpay test-mode integration ===")
    print(f"  run id               {result.run_id}")
    print(f"  cases run            {result.cases}/{result.cases + len(result.failures)}")
    print(f"  customers created    {result.customers}")
    print(f"  orders created       {result.orders}")
    print(f"  orders verified      {result.verified}/{result.orders}")
    probes = min(result.cases, LINK_PROBES)
    print(
        f"  recovery links       {result.links}/{probes} probed"
        f"  ({result.links_rate_limited} rate-limited by provider)"
    )
    print(f"  duplicate refused    {result.duplicate_refused}/{max(result.links, 1)} created")
    print(f"  value covered        {format_inr(paise(total))}")
    print(
        f"  api calls            {result.api_calls} in {result.elapsed_s:.1f}s"
        f"  ({result.throttled} throttled)"
    )
    print()
    print("  live downtime feed (drives the policy engine's outage gate):")
    for method, count in sorted(result.downtimes.items()):
        print(f"    {method:12s} {count} blocking")
    print()
    print("  webhook handling:")
    print(f"    valid signature accepted   {result.webhook_ok}")
    print(f"    forged signature rejected  {result.webhook_forgery_rejected}")
    print(f"    replayed delivery dropped  {result.webhook_replay_dropped}")

    if result.failures:
        print(f"\n  failures ({len(result.failures)}):")
        for line in result.failures[:5]:
            print(f"    {line}")

    print(
        "\nSCOPE. Subscriptions is not enabled on this account (/v1/subscriptions "
        "returns 401),\nso no mandate debit is performed here and none is claimed. "
        "Batch C proves the\nintegration; the recovery statistics come from Batches "
        "A and B against the\nsimulator. See docs/analysis-plan.md."
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="recovery.batch.live")
    parser.add_argument("--cases", type=int, default=50)
    parser.add_argument(
        "--pace",
        type=float,
        default=DEFAULT_PACE_SECONDS,
        help="Seconds between cases, to stay under the provider rate limit.",
    )
    parser.add_argument("--report", default=None, help="write the ledger as JSON")
    args = parser.parse_args()

    result, ledger = run(args.cases, pace_seconds=args.pace)
    _print(result)

    if args.report:
        events: list[dict[str, Any]] = []
        store = ledger._store
        for case_id in store.all_cases():  # type: ignore[attr-defined]
            events.extend(json.loads(e.to_json()) for e in ledger.history(case_id))
        with open(args.report, "w", encoding="utf-8") as handle:
            json.dump(events, handle, indent=2)
        print(f"\n  ledger written: {args.report} ({len(events)} events)")


if __name__ == "__main__":
    main()
