# Analysis Plan (Pre-Registration)

**Project:** AI Revenue Recovery — subscription / e-mandate dunning agent
**Written:** 2026-08-23
**Status:** Written and pushed **before any batch data was generated.**

> **On how this is witnessed.** A git author date is settable and therefore proves nothing on its
> own. The claim that this plan predates the data rests on this file being pushed to the public
> repository before the first generator run, which is externally observable in the repo's push
> history. If you are evaluating this submission and that ordering matters to you, check the push
> timestamps rather than the commit dates.

---

## 1. Questions

- **R1 — Does the recovery agent beat the platform default?**
  Measured on a realistic population of failed e-mandate charges.
- **R2 — Does the LLM component pay for itself?**
  Measured only on the case population where the LLM is actually applied (the "tail").

These are separate questions with separate batches because one batch cannot power both. R2 is nested
inside R1's treatment arm.

## 2. Batches

| Batch | N (target) | Composition | Randomization | Serves |
|---|---|---|---|---|
| **A — Population** | ≥2,000, run as large as wall-clock allows; actual N reported | Realistic decline-class mix (§2.1) | 50/50 treatment vs holdout | R1 |
| **B — Tail-enriched** | ≥800 | Oversampled tail subtypes (§2.2) | 50/50 agent-loop vs deterministic fallback | R2 |
| **C — Real** | ~50 | Razorpay test mode, whatever `Charge This Now` produces | none | Integration proof only — **no statistical claims** |

**Why Batch A and Batch C are structurally different paths.** Razorpay's test-mode manual charge
(`Charge This Now`) is a dashboard button operated one case at a time. The real-integration path
therefore *cannot* produce a batch of the size statistics require — not as a matter of effort, but
by construction. Batch C proves the integration is real; Batch A provides the statistics. These are
different claims and are reported separately. No number from Batch C is presented as evidence of
effect size.

**Why 50/50 rather than the production-conventional 85/15.** A holdout in production costs real
revenue, which is why holdouts are kept small. A synthetic control case costs nothing, so the only
argument for skewing is distributional realism — and power is worth more than realism here. In
production this would run at 85/15 and detect correspondingly less.

**Batch A is cheap to scale.** Only tail-routed treatment cases invoke an LLM (~20% of treatment),
so N is close to a config value. It will be run as large as the wall clock permits and the actual N
reported, not the target.

### 2.1 Batch A decline-class mix

The generator's distribution is a **stated assumption, not ground truth.** It is calibrated to be
plausible for Indian card e-mandate recurring debits and is published in the generator config so it
can be criticised. No claim is made that it matches any real merchant's distribution.

| Class | `reason` | Share |
|---|---|---|
| SOFT | `insufficient_funds` | 40% |
| SOFT | `card_declined` / `payment_failed` | 10% |
| SOFT | `authentication_failed` | 10% |
| SOFT | `transaction_limit_exceeded` | 5% |
| SOFT | `payment_risk_check_failed` | 3% |
| DOWNTIME | `bank_technical_error` / `gateway_technical_error` | 15% |
| HARD | `card_expired` | 8% |
| HARD | `debit_instrument_blocked` / `_inactive` / `card_not_enrolled` | 7% |
| — | other / unmapped | 2% |

### 2.2 Batch B tail subtypes

A case is **tail-eligible** if it meets any of:

- `ambiguous_diagnosis` — top diagnosis confidence below threshold, or conflicting signals
- `high_value` — amount above the 90th percentile of the population
- `inbound_freetext` — customer replied with unstructured text

Batch B oversamples these relative to their natural prevalence. **Natural prevalence per subtype is
measured from Batch A and recorded**, because it is required for the composition in §5.

## 3. Assignment

- **R1:** every case, at detection time, assigned `treatment | control` 50/50. Stratified by decline
  class and amount decile so the arms are comparable.
- **R2:** within the treatment arm, once the router marks a case tail-eligible, assign
  `agent_loop | deterministic_fallback` 50/50. Cases routed to fallback remain in R1's treatment arm.

Consequence, stated up front: **R1's treatment arm is a mixture** of agent-handled and rules-handled
cases. The R1 headline is therefore a blended system-level number, not an LLM number. R2 is the LLM
number.

Assignment is by seeded PRNG keyed on case id; the seed is recorded so every batch is reproducible.

## 4. Metrics

### 4.1 Primary — one, declared in advance

**Incremental ₹ recovered in Batch A.**

```
recovered(case)  := a payment for this invoice reaches `captured`
                    within the case observation window
recovery_rate(a) := recovered_cases(a) / cases(a)
lift             := recovery_rate(treatment) − recovery_rate(control)
incremental_₹    := lift × n(treatment) × mean_recovered_paise(treatment)
```

**Case-level recovery carries no action attribution.** Whether the invoice got paid inside the
window is the entire question. An earlier draft required "no intervening action," which would have
excluded the normal multi-touch dunning shape (notice → retry → link → paid) — precisely the cases
that matter — and undercounted recovery on them.

### 4.2 Secondary — one

**Tail lift in Batch B**: `recovery_rate(agent_loop) − recovery_rate(deterministic_fallback)`, and
its money equivalent.

### 4.3 Descriptive — no significance claims attached

Everything below is reported for understanding, **not** for inference. A striking value here is
something to notice and flag as hypothesis-generating; it is not a result and will not be described
as one:

- recovery rate by decline class (hard / soft / downtime)
- policy blocks by gate (8 gates)
- stopping-rule firings by rule
- actions per recovered case
- ₹ cost per ₹ recovered
- mean time-to-recovery
- `outcome_source ∈ {attributed, organic, none}` distribution

This split exists because the descriptive list is long, and a long list of slices with no declared
hierarchy is p-hacking through the side door — the exact failure the pre-registration is meant to
prevent.

### 4.4 Action-level attribution — cost slices only

Needed only to divide cost across actions, never for the lift metrics.

```
attributed_action(case) := the last action whose attribution window
                           was still open when the payment captured
```

Windows: **72h** for messaging; **link expiry** for payment links. Arbitrary but stated.

## 5. Composing R2 back to the population

Batch B is enriched, so its aggregate lift does not generalise. Enrichment changes the mix *within*
the tail, so a single `lift × prevalence` product would be averaging over the wrong distribution.

Compute lift **within subtype**, then reweight by natural prevalence measured in Batch A:

```
lift_natural_tail = Σ_s  w_s · lift_s
    w_s   = natural prevalence of subtype s among tail cases (from Batch A)
    lift_s = recovery_rate_agent(s) − recovery_rate_fallback(s)

population_rate_contribution = lift_natural_tail × tail_prevalence
```

**Rate and money must be composed separately.** High-value enrichment makes them diverge — a rate
lift concentrated in high-value cases is worth more money than the same rate lift spread evenly. So:

```
money_contribution = tail_prevalence × Σ_s  w_s · lift_s · mean_recovered_paise_s
```

Both are reported. Neither is presented as the other.

## 6. Observation window and arm symmetry

- Every case gets an **identical observation window** in both arms, measured from detection. Not
  from first action — the control arm has no first action, and anchoring on it would give the
  holdout a shorter effective window and inflate the baseline.
- The **settle delay** (`cooling` state: hold after `payment.failed`, re-check payment status before
  acting) applies to **both arms**. `payment.failed` followed by `payment.captured` on the same
  order is documented-normal on UPI; without a symmetric cooling state the control arm accrues
  spurious "self-cures" and the measured lift shrinks for a reason that has nothing to do with the
  agent.

## 7. Exclusions

Declared in advance. Excluded cases are counted and reported, never silently dropped:

- cases whose observation window had not closed when the batch was cut
- cases where the simulated provider errored in a way that produced no terminal state
- duplicate cases arising from replayed webhooks (deduped on event id)

Excluded for **cause**, and reported separately as the honest exception list:

- cases the policy engine refused entirely (e.g. no consent on record)
- cases where the customer opted out before any action

## 8. Statistics

- Two-proportion comparison, α = 0.05, 95% CIs on all lift figures.
- Power at α=0.05 / 80% uses n per arm ≈ 16·p̄(1−p̄)/δ².
  - Batch A at 1,000/arm, p̄≈0.5 → detectable δ ≈ **6.3pt**
  - Batch B at 400/arm, p̄≈0.25 → detectable δ ≈ **8.7pt**
- **If a confidence interval straddles zero, that is the reported result.** No point estimate will
  be presented without its interval.

## 9. Stopping point

The batch is cut when all observation windows close or the wall-clock budget expires, whichever is
first. **The analysis is run once.** If a bug is found after the run, the fix is documented, the
batch is regenerated from the recorded seed, and *both* results are reported.

## 10. The null result, decided in advance

Dunning's dominant lever is retry timing, and timing is rules. There is a live possibility that R2
shows the LLM adds little or nothing measurable.

**That is a finding, not a failure**, and it will be reported as the headline of R2 if it occurs:

> *"I built the agent loop, measured it honestly against a deterministic fallback on the population
> where it was actually applied, and the lift was [X]pt (95% CI [a, b]). The dominant lever in this
> problem is decline-conditional retry timing, which is rules. Here is the narrow band where the
> model does pay for itself, and here is what it costs per case."*

This paragraph is written on 2026-08-23, before any data exists, so that it does not have to be
written under deadline pressure with a video unshot.

## 11. What would falsify the design

- R1 lift CI straddling zero → the agent does not beat the platform default on this population.
- Recovery rate on HARD-class declines materially above zero in the treatment arm → the decline
  taxonomy is wrong, or the classifier is leaking retries onto instruments that cannot succeed.
- Policy-gate block count at zero → the compliance engine is decorative, not load-bearing.
- Cost per ₹ recovered > 1 → the agent destroys value.
