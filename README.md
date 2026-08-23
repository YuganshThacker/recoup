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
  The model proposes; it never executes, never emits an amount, and never writes copy —
  its output schema has no field for either.
- **Compliance is code, not prose.** Eight gates — consent, suppression, e-mandate
  notice/AFA, attempt budget, quiet hours, cooldown, DLT template, channel economics —
  each able to refuse an action and say why. Refusals are structured, so the agent
  re-plans against them instead of stalling.
- **Honest measurement.** Randomised holdout for the system (R1), randomised split
  *within the tail* for the model (R2). Incremental ₹, with confidence intervals, and a
  self-cure baseline both arms share.

## Running it

```bash
python -m recovery.batch                     # rules only
python -m recovery.batch --agent scripted    # exercises the agent loop, no spend
python -m recovery.batch --agent live        # real model calls; needs the `agent` extra
```

`--agent off` and `--agent scripted` need no dependencies at all. `--agent live` needs
`pip install -e ".[agent]"` and credentials.

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
