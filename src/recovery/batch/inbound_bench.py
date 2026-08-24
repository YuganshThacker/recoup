"""Benchmark: does a model read customer messages better than keywords?

    python -m recovery.batch.inbound_bench --model gpt-4.1-mini

R2 asked whether the model beats deterministic rules at *timing* and answered
no, decisively. This asks the opposite question on the capability rules
genuinely lack, against a keyword baseline written in good faith rather than to
lose.

Four things are scored, because "accuracy" alone hides where a system fails:

``intent``       exact match on the closed enum
``date``         the promised date, resolved -- and a spurious date counts against
``suppression``  whether an explicit stop request was recognised
``policy``       whether the reading produces the *same policy facts* as the label

The last is the one that matters operationally. Getting the intent right but the
date wrong still schedules a retry on the wrong day, and getting suppression
wrong contacts someone who asked not to be. ``policy`` is the only column that
scores the whole reading.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from math import comb

from recovery.agent.inbound import (
    InboundExtractor,
    InboundReading,
    keyword_reading,
)
from recovery.env import load_dotenv
from recovery.sim.inbound_corpus import CATEGORIES, CORPUS, TODAY, LabelledMessage

NOW = datetime(TODAY.year, TODAY.month, TODAY.day, 10, 0, tzinfo=UTC)


@dataclass
class Tally:
    """Correct counts for one approach, per metric, per category."""

    intent: dict[str, list[int]] = field(default_factory=lambda: defaultdict(lambda: [0, 0]))
    date: dict[str, list[int]] = field(default_factory=lambda: defaultdict(lambda: [0, 0]))
    suppression: dict[str, list[int]] = field(default_factory=lambda: defaultdict(lambda: [0, 0]))
    policy: dict[str, list[int]] = field(default_factory=lambda: defaultdict(lambda: [0, 0]))
    misreads: list[str] = field(default_factory=list)
    policy_correct: list[bool] = field(default_factory=list)

    def score(self, label: LabelledMessage, reading: InboundReading) -> None:
        truth = _expected_facts(label)
        got = reading.policy_facts(now=NOW)

        for metric, correct in (
            (self.intent, reading.intent is label.intent),
            (self.date, reading.promised_date == label.promised_date),
            (self.suppression, reading.requests_no_retry == label.requests_no_retry),
            (self.policy, got == truth),
        ):
            metric[label.category][0] += int(correct)
            metric[label.category][1] += 1

        self.policy_correct.append(got == truth)

        if reading.intent is not label.intent:
            self.misreads.append(
                f"{label.category:9s} {label.text[:52]!r} "
                f"-> {reading.intent.value} (expected {label.intent.value})"
            )

    def rate(self, metric: dict[str, list[int]]) -> float:
        ok = sum(v[0] for v in metric.values())
        n = sum(v[1] for v in metric.values())
        return ok / n if n else 0.0


def _expected_facts(label: LabelledMessage) -> dict[str, object]:
    """The policy facts a perfect reading of this message would produce."""
    return InboundReading(
        intent=label.intent,
        promised_date=label.promised_date,
        requests_no_retry=label.requests_no_retry,
        confidence=1.0,
        verbatim="",
    ).policy_facts(now=NOW)


def mcnemar(model: list[bool], baseline: list[bool]) -> tuple[int, int, float]:
    """Exact McNemar test on the paired policy-facts outcomes.

    The two approaches read the *same* messages, so an unpaired two-proportion
    test would overstate the evidence by ignoring that most cases are decided
    identically. Only the disagreements carry information: b is where the model
    is right and the baseline wrong, c the reverse.

    Returns (b, c, two-sided exact p).
    """
    b = sum(1 for m, k in zip(model, baseline, strict=True) if m and not k)
    c = sum(1 for m, k in zip(model, baseline, strict=True) if k and not m)
    n = b + c
    if n == 0:
        return b, c, 1.0
    lo = min(b, c)
    tail = sum(comb(n, i) for i in range(lo + 1)) / (2**n)
    return b, c, min(1.0, 2 * tail)


def _table(name: str, tally: Tally) -> None:
    print(f"\n  {name}")
    print(f"    {'category':11s} {'intent':>8s} {'date':>8s} {'stop':>8s} {'policy':>8s}")
    for category in CATEGORIES:
        cells = []
        for metric in (tally.intent, tally.date, tally.suppression, tally.policy):
            ok, n = metric[category]
            cells.append(f"{ok}/{n}" if n else "-")
        print(f"    {category:11s} " + " ".join(f"{c:>8s}" for c in cells))
    print(
        f"    {'OVERALL':11s} "
        + " ".join(
            f"{tally.rate(m):>7.0%} "
            for m in (tally.intent, tally.date, tally.suppression, tally.policy)
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="recovery.batch.inbound_bench")
    parser.add_argument("--model", default="gpt-4.1-mini")
    parser.add_argument("--provider", choices=("openai", "anthropic"), default="openai")
    parser.add_argument("--workers", type=int, default=12)
    args = parser.parse_args()

    load_dotenv()
    if args.provider == "openai":
        from recovery.agent.openai_client import OpenAIClient

        client = OpenAIClient(model=args.model)
    else:
        from recovery.agent.anthropic_client import AnthropicClient

        client = AnthropicClient(model=args.model)  # type: ignore[assignment]

    baseline, model = Tally(), Tally()
    extractor = InboundExtractor(client)

    # Order matters: the tallies are paired index-by-index for McNemar, so the
    # readings are collected in corpus order regardless of completion order.
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        readings = list(pool.map(lambda m: extractor.read(m.text, today=TODAY)[0], CORPUS))

    for label, reading in zip(CORPUS, readings, strict=True):
        baseline.score(label, keyword_reading(label.text, today=TODAY))
        model.score(label, reading)

    print(f"=== INBOUND UNDERSTANDING - {len(CORPUS)} labelled messages ===")
    print(f"  model: {args.model}   reference date: {TODAY} ({TODAY.strftime('%A')})")
    _table("keyword baseline", baseline)
    _table(f"model ({args.model})", model)

    print("\n  delta (model - baseline)")
    for name, m, b in (
        ("intent", model.intent, baseline.intent),
        ("date", model.date, baseline.date),
        ("suppression", model.suppression, baseline.suppression),
        ("policy facts", model.policy, baseline.policy),
    ):
        print(f"    {name:13s} {model.rate(m) - baseline.rate(b):+.0%}")

    wins, losses, p_value = mcnemar(model.policy_correct, baseline.policy_correct)
    print("\n  paired significance on policy facts (McNemar, exact)")
    print(f"    model right / baseline wrong   b = {wins}")
    print(f"    baseline right / model wrong   c = {losses}")
    print(
        f"    two-sided p                    {p_value:.4f}"
        + ("  significant at 0.05" if p_value < 0.05 else "  NOT significant at 0.05")
    )

    print(
        f"\n  model usage: {extractor.calls} calls, "
        f"{extractor.input_tokens + extractor.output_tokens:,} tokens, "
        f"{extractor.fallbacks} fell back to keywords"
    )
    if extractor.errors:
        print(f"  errors: {extractor.errors[:3]}")

    if model.misreads:
        print(f"\n  model misreads ({len(model.misreads)}):")
        for line in model.misreads[:8]:
            print(f"    {line}")
    if baseline.misreads:
        print(f"\n  baseline misreads ({len(baseline.misreads)}):")
        for line in baseline.misreads[:8]:
            print(f"    {line}")

    print(
        "\nSCOPE. The corpus and its labels were written by the same author as the "
        "system\nunder test, which is stated in sim/inbound_corpus.py rather than "
        "buried. The\nbaseline is deliberately competent; where keywords suffice it "
        "is meant to score."
    )


if __name__ == "__main__":
    main()
