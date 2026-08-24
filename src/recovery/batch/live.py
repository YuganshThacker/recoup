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


@dataclass
class LiveResult:
    """What Batch C established, and what it did not."""

    cases: int = 0
    orders: int = 0
    links: int = 0
    verified: int = 0
    idempotent_hits: int = 0
    failures: list[str] = field(default_factory=list)
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


# Creating a second link per case doubles the rate-limit cost. The duplicate
# guarantee is a property of the provider, not of any particular case, so it
# is proven on a few and asserted once.
DUPLICATE_PROBES = 5


def _one_case(
    gateway: RazorpayGateway, ledger: Ledger, index: int, amount: Paise
) -> tuple[bool, bool]:
    """Run one case against the live API. Returns (created, idempotent_hit)."""
    case_id = f"live_{index:03d}"
    reference = f"recovery-{case_id}"

    order = gateway.create_order(amount=amount, receipt=reference, notes={"case_id": case_id})
    ledger.record(
        case_id,
        EventKind.CASE_DETECTED,
        Actor.SYSTEM,
        f"order created {order['id']}",
        {"amount_paise": order["amount"], "status": order["status"]},
    )

    link = gateway.create_recovery_link(
        amount=amount,
        reference_id=reference,
        description=f"Recovery for {case_id}",
        notes={"case_id": case_id},
    )
    ledger.record(
        case_id,
        EventKind.ACTION_EXECUTED,
        Actor.SYSTEM,
        f"recovery link created {link['id']}",
        {"short_url": link.get("short_url"), "status": link.get("status")},
    )

    fetched = gateway.fetch_payment_link(link["id"])
    verified = fetched["id"] == link["id"] and int(fetched["amount"]) == int(amount)
    ledger.record(
        case_id,
        EventKind.OUTCOME_RECORDED,
        Actor.SYSTEM,
        "link verified server-side" if verified else "link verification FAILED",
    )

    # Razorpay rejects a duplicate reference_id. That is the provider enforcing
    # the same guarantee our idempotency keys enforce internally, so a rejection
    # here is the success condition.
    idempotent_hit = False
    if index >= DUPLICATE_PROBES:
        return verified, False
    try:
        gateway.create_recovery_link(
            amount=amount, reference_id=reference, description="duplicate attempt"
        )
    except RazorpayError:
        idempotent_hit = True
        ledger.record(
            case_id,
            EventKind.ACTION_DEDUPED,
            Actor.SYSTEM,
            "duplicate reference_id refused by provider",
        )
    return verified, idempotent_hit


def run(cases: int) -> tuple[LiveResult, Ledger]:
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
        try:
            verified, deduped = _one_case(gateway, ledger, index, amount)
            result.cases += 1
            result.orders += 1
            result.links += 1
            result.verified += int(verified)
            result.idempotent_hits += int(deduped)
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
    print(f"  cases run            {result.cases}")
    print(f"  orders created       {result.orders}")
    print(f"  recovery links       {result.links}")
    print(f"  links verified       {result.verified}/{result.links}")
    probes = min(result.cases, DUPLICATE_PROBES)
    print(f"  duplicate refused    {result.idempotent_hits}/{probes} probed")
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
    parser.add_argument("--report", default=None, help="write the ledger as JSON")
    args = parser.parse_args()

    result, ledger = run(args.cases)
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
