# Results

Every figure here comes from a run whose raw output is committed in `reports/`.
The metric hierarchy — one primary, one secondary, everything else descriptive —
was fixed in [`analysis-plan.md`](analysis-plan.md) and pushed to the public
repository before any batch data existed.

**These are simulation results.** The world model in `src/recovery/sim/world.py`
encodes the hypothesis that retry timing matters, so these runs cannot confirm
that hypothesis. What they establish is narrower and still worth having: that
the policy exploits the structure it is given, that the machinery runs end to
end at batch scale, and that the measurement apparatus works — including
detecting when the system's own agent makes things worse.

---

## R1 — does the recovery system beat the platform default?

**Batch A, n=900, seed 20260823.** Raw output: `reports/run_r1_population.txt`.

| | treatment | holdout | delta |
|---|---|---|---|
| **recovery rate** | 0.7314 | 0.5158 | **+0.2156** |

95% CI **[+0.1538, +0.2774]** · incremental **₹96,908.74** · cost per rupee **₹0.0028**

The control arm reproduces the platform default: a pre-debit notice, then a
retry, repeated daily until three attempts are spent. It is decline-blind — it
makes the same three attempts on an expired card as on an account that was
briefly short of funds.

### Where the lift comes from

| decline class | treatment | holdout | delta | n |
|---|---|---|---|---|
| soft | 0.797 | 0.481 | **+0.316** | 320/318 |
| unknown | 0.250 | 0.000 | +0.250 | 12/5 |
| hard | 0.217 | 0.241 | −0.025 | 60/58 |
| downtime | 0.970 | 1.000 | −0.030 | 66/61 |

Effectively all of it is on **soft declines**, which is the mechanism the design
predicts: those are the cases where a retry can succeed and the only question is
when. `insufficient_funds` recovered at 0.859 against 0.444 while using *fewer*
attempts (1.66 vs 2.57) — better timing, less load on the issuer, more money.

Two results that do not flatter the design, kept because they are true:

* **Downtime shows no advantage** (−0.030). Waiting out an outage buys nothing
  over the platform's fixed retry, because the mandatory 24-hour notice already
  outlasts a typical outage. A piece of the design rationale that the data does
  not support.
* **Hard declines are flat** (−0.025). This is the correct outcome, not a
  failure: no debit can succeed on a dead instrument, so the ~22% that recover
  do so through instrument repair or self-cure in both arms. The system's
  contribution is *withholding* 103 attempts that could not have worked.

### The baseline that makes the number honest

Organic recoveries — customers who paid unprompted — were **46 in treatment and
45 in control**. A balanced self-cure rate across arms is what makes the
comparison sound. An earlier build failed this: the control arm exhausts its
attempts sooner, stopped being watched sooner, and was therefore credited with
fewer organic payments. That inflated the measured lift for a purely
bookkeeping reason, and is now asserted by a test.

---

## R2 — does the model beat the deterministic fallback?

**Batch B, tail-enriched, n=700, seed 20260824, `gpt-4.1-mini`.**
Raw output: `reports/run_r2_ablation.txt`.

Cases reach the tail *because they are hard* — ambiguous diagnosis, high value,
an inbound reply. Comparing agent-handled tail cases against the easy
rules-handled population would measure the router, not the model. So
tail-eligible cases are randomised again, half to the agent loop and half to the
deterministic fallback, both inside R1's treatment arm.

### The result

**Lift: −0.2124**, 95% CI **[−0.3326, −0.0922]**, n=120/119.
**The interval excludes zero.**

(The per-arm absolute rates are not quoted: the run printed the difference and
the arm sizes, not the two rates, and a number that was not printed is not
evidence. The CLI now prints them so future runs can be cited directly.)

| subtype | delta | 95% CI | n |
|---|---|---|---|
| inbound_freetext | **−0.3682** | [−0.5599, −0.1765] | 40/44 |
| high_value | −0.2208 | [−0.5590, +0.1175] | 14/11 |
| ambiguous_diagnosis | −0.0843 | [−0.2304, +0.0618] | 66/64 |

Composed to population by subtype prevalence: **−0.0701 rate**, **−₹2,021.03 per
treated case**.

### The finding

**The model does not merely fail to add value. It destroys it**, by about 21
recovery points on the cases it handles.

The mechanism is visible in a behavioural probe run separately against the same
model (8 scenarios, one call each). It gets the safety-critical calls right — it
never proposed a debit on a hard decline, correctly waited out an outage,
correctly stopped when an instrument-repair request went unanswered. What it
gets wrong is timing: it systematically over-waits, proposing `wait +360h` on a
case whose pre-debit notice had already matured, burning 15 of a 21-day window
and letting the notice go stale. In a problem where the entire lift comes from
attempting at the right moment, an agent that defers is an agent that loses
money.

This is the pre-registered null result, and it landed stronger than the
pre-registration anticipated:

> *"Dunning's dominant lever is retry timing, and timing is rules. If the
> ablation shows the model adds little, that is the finding."*

### Why this number is conservative

116 of 1,114 model calls hit the run's token ceiling and fell back to rules, so
roughly 12% of the agent arm was partly rules-handled. Dilution pulls the
measured effect *toward* zero. The true effect is therefore likely more
negative than −21pt, not less.

### Three earlier runs, all invalidated

| run | tail n | model-call failure rate | R2 | verdict |
|---|---|---|---|---|
| 1 | 300 | 84% | −0.119 | **VOID** |
| 2 | 1400 | 41% | −0.018 | **VOID** |
| 3 | 1200 | 27% | −0.032 | **VOID** |
| 4 | 700 | **11.9%** | **−0.212** | valid |

Runs 1–3 exhausted their token budgets, so most of the "agent" arm was actually
the deterministic fallback — a comparison of rules against rules wearing an
agent label. The harness refuses to present those as an ablation:

```
R2 ABLATION VOID: 41% of model calls failed ({'token_budget_exhausted': 480, ...}),
so the agent arm is mostly the deterministic fallback. The R2 numbers below are
not an ablation and must not be reported as one.
```

That check exists because a failed run still completes, still prints a lift, and
still attaches a confidence interval to it. On a deadline that is precisely the
number that gets shipped by accident.

### What R2 does *not* test

The `inbound_freetext` subtype is a **label, not text**. The simulator marks a
case as having received a customer reply; it never generates the reply. So the
strongest theoretical argument for a model in this loop — parsing free-text
promises to pay into structured commitments — is **untested here**, and the
−0.368 on that subtype reflects timing behaviour rather than any failure of
language understanding.

---

## R3 — does the model read customer messages better than keywords?

**153 labelled messages · `gpt-4.1-mini` · reference date 2026-08-25.**
Raw output: `reports/run_inbound_bench.txt`.

R2 asked whether the model beats rules at *timing* and answered no. This asks the
opposite question on the capability rules genuinely lack. No rule turns *"can't
pay today, salary comes Friday, please stop retrying"* into a promise dated to
Friday **and** a suppression flag.

| metric | keyword baseline | model | delta |
|---|---|---|---|
| intent | 80% | 91% | +11% |
| promised date | 84% | 95% | +10% |
| suppression | 97% | 99% | +1% |
| **policy facts** | **78%** | **90%** | **+12%** |

**McNemar (exact, paired): b=27, c=8, p=0.0019.** Significant at 0.05.

Stable across runs: policy facts 90%, 90%, 91%; p = 0.0013, 0.0013, 0.0019.

### Why McNemar and not a two-proportion test

Both approaches read the *same* 153 messages. An unpaired test would count the
~110 cases they decide identically as evidence, overstating it. Only the
disagreements carry information.

### Why `policy facts` is the metric that matters

Intent is a proxy. Getting the intent right and the date wrong still schedules a
retry on the wrong day; getting suppression wrong contacts someone who asked not
to be. `policy facts` scores the whole reading — the exact dict that lands in
`PolicyContext` — and is the only column that reflects what the system does.

### Where each one loses

| | baseline | model |
|---|---|---|
| relative dates | 15/26 | **23/26** |
| traps (misleading keyword) | 10/28 | **21/28** |
| multi-intent | **12/12** | 9/12 |

The failure modes are structurally different. Keywords break on phrasing and on
negation — *"I already paid last month's invoice but not this one, I'll clear
this by Friday"* reads as a dispute. The model breaks on our multi-intent
labelling convention, where the baseline's keyword precedence happens to encode
the right answer by accident of ordering.

### Two corrections made while running this

Both moved the result *against* the model, and both are why the number is
believable:

1. **n=47 gave p=0.2266 — not significant.** The first corpus was too small.
   Reporting +11% from it would have held the flattering result to a weaker
   standard than R2, which was held to confidence intervals and three rejected
   runs. The corpus was expanded to 153.
2. **The baseline was handicapped.** It discarded an extracted date whenever the
   intent was not `promise_to_pay`, contradicting the documented multi-intent
   convention, which cost it 12 cases outright. Fixed; the effect fell from +19
   to +12 and p from <0.0001 to 0.0019.

A third disclosure: the multi-intent convention was added to the extraction
prompt *after* an initial run where the model read such messages as promises.
That is post-hoc. The baseline already encoded the same precedence, so stating
it removed an asymmetry rather than creating one — but it is post-hoc and is
recorded as such.

### What this does not establish

The corpus and its labels were written by the same author as the system under
test. That is a real weakness, stated in `sim/inbound_corpus.py`. Three things
push against it: the baseline is deliberately competent (80% intent, 97%
suppression) and pinned by a test at >70%; most of the corpus is ordinary
messages either approach should read; and every label is checkable from the text
alone. It is not a substitute for real customer messages.

### The two results together

The model **loses at timing by 21 points** and **wins at understanding by 12**.
That is not a contradiction — it is the reason the architecture routes rather
than delegates. Rules keep control of scheduling; the model is used where
language understanding is the actual task, and its output lands as *facts* in a
context the policy engine already gates.

---

## Batch C — the live Razorpay integration

**50 cases against the live test API.** Ledger: `reports/batch_c_ledger.json`.

```
cases run            50/50
customers created    50
orders created       50
orders verified      50/50      fetched back; amount and receipt matched
recovery links        0/5 probed  (5 rate-limited by provider)
api calls           176 in 98.4s  (20 throttled)

live downtime feed   card 5 · netbanking 5 · upi 2 · fpx 2 blocking
webhook: valid accepted · forged rejected · replay dropped
```

This is a different claim from R1 and R2 and is never merged with them. Razorpay
test mode drives a manual charge from a dashboard button, one case at a time,
and **Subscriptions is not enabled on this account** (`/v1/subscriptions`
returns 401) — so the real path cannot produce a batch of the size the statistics
require, and no mandate debit is performed. `RazorpayGateway.charge()` refuses
rather than substituting a payment link, because reporting a
customer-authenticated payment as an automatic debit would make the recovery
numbers mean something else.

The **downtime feed is live**. Razorpay publishes active outages keyed by bank,
issuer, VPA handle and card network; the policy engine's outage gate consults
them, so that refusal follows real issuer state rather than a flag we invented.

Recovery links show 0/5 in this run because the account's quota was spent. 27
were created successfully in earlier runs the same day and the path is covered
by tests. The zero is left visible.

---

## Limitations

1. **Simulation.** R1 and R2 run against a world model that encodes the timing
   hypothesis. They cannot confirm it. Real validation needs merchant data.
2. **The generator's distribution is an assumption**, published in
   `sim/world.py` so it can be argued with. It is not calibrated to any real
   merchant.
3. **R2 is powered to detect roughly 18 points** at n=120/119. It found 21. A
   smaller true effect would not have been visible.
4. **Inbound free text is never generated**, so the model's parsing ability is
   untested.
5. **No mandate debit was ever executed**, against Razorpay or anywhere else.
6. **One model, one prompt.** `gpt-4.1-mini` was chosen on measured grounds —
   `gpt-5-mini` spent its entire 1,024-token output budget reasoning and
   returned nothing usable in 16.6s, against 88 tokens in 2.2s — but a different
   model or a better prompt might do better. The claim is about this
   configuration, not about language models.

## The honest conclusion

The system beats the platform default by **+21.6 points and ₹96,908 incremental**
on a 900-case batch. Substantially all of that comes from decline-conditional
retry timing, which is **rules**.

The model, measured on the population where it was actually applied, is **worse
than those rules by 21 points** at choosing when to act — and **better than a
competent keyword baseline by 12 points** at reading what a customer wrote.

So the correct product decision is not "use AI" or "don't". It is: keep
deterministic rules in charge of timing, and use the model for the one job rules
cannot do. We can say that with numbers attached because the architecture routes
the model to a bounded tail and measures it there, instead of assuming it helps.
