# Bounded Revenue Recovery Agent

Razorpay AI Buildathon — Track 03, AI Revenue Recovery.
Vertical: **subscription / e-mandate dunning.**

```
Payment Event → Case → Cooling → Diagnosis → Policy Gate → Execution
              → Outcome → Audit Ledger + Measurement
```

When a recurring charge fails, the agent works out *why*, decides whether recovery is
appropriate at all, chooses the best **permitted** action, executes it safely, and then
proves whether that action recovered **incremental** revenue.

## Design commitments

- **Deterministic control, probabilistic core.** A policy engine gates every money action.
  The LLM proposes; it never executes and never emits an amount.
- **Compliance is code, not prose.** Eight gates — consent, quiet hours, DLT template,
  channel economics, attempt budget, e-mandate notice/AFA, suppression, cooldown — each
  able to refuse an action and log why.
- **Honest measurement.** Randomised holdout for the system, randomised split *within the
  tail* for the model. Incremental ₹, with confidence intervals.

## Pre-registration

[`docs/analysis-plan.md`](docs/analysis-plan.md) was written and pushed **before any batch
data was generated**. Primary metric, attribution windows, exclusions and the null-result
framing are all fixed in advance.

## Docs

- [Domain brief](docs/research/2026-08-23-revenue-recovery-domain-brief.md) — Razorpay substrate,
  decline taxonomy, Indian compliance envelope, measurement problem.
- [Analysis plan](docs/analysis-plan.md) — pre-registered experiment design.

## Development

```bash
python3 -m pytest -q
python3 -m ruff check . && python3 -m ruff format --check .
```
