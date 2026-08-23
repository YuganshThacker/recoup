# AI Revenue Recovery — Domain Brief

Razorpay AI Buildathon, Track 03. Research compiled 2026-08-23.
Purpose: know the substrate well enough to defend every design decision in a panel interview.

> Confidence markers used throughout: **[doc]** = from Razorpay/regulator docs, **[press]** = from
> reporting or vendor blogs (directionally right, don't quote as gospel), **[verify]** = believed
> true, must be checked in test mode before it goes in a pitch.

---

## 1. What the track actually asks, and how it is scored

Verbatim from the track page:

> Build an agent that detects revenue at risk, determines the right intervention, and executes a
> bounded recovery workflow: from payment failures and checkout abandonment to overdue receivables.

**The bar** (this is the rubric, treat it as the spec):

> Don't just identify the problem. Show **measured money recovered across a batch**, with
> **compliant escalation**, **stopping rules**, and an **audit trail**.

Four nouns. Each is a hard requirement, and each is where most submissions will fail:

| Bar clause | What a weak submission does | What it actually demands |
|---|---|---|
| measured money recovered | "our agent detected 40 at-risk payments" | ₹ figure, across a batch, with a **counterfactual** |
| across a batch | one cherry-picked demo case | N≥50 cases run end-to-end, aggregate stats |
| compliant escalation | blasts WhatsApp at 2am | channel/timing/consent gated by Indian law |
| stopping rules | retries forever | caps, cooldowns, suppression, terminal states |
| audit trail | console logs | append-only, replayable, every decision explains itself |

Cross-track language from the same page is a strong tell about what Razorpay's engineers care about
— Track 01's bar reads *"Every money action explainable, bounded and gated. Show the audit trail and
one failure handled gracefully."* Track 04's reads *"Throughput plus measured accuracy plus an honest
exception list. One cherry-picked match proves nothing."* **[doc]**

Read together, the house style is: *deterministic control around a probabilistic core, honest
numbers including the ones that make you look bad.* Build for that.

**Selection process:** public GitHub repo + 5-minute pitch video + architecture doc → panel
interview. No aptitude test, no GD, no resume screen. Offer is ₹75,000/month, 6 or 12 months,
in-person Bangalore from September. Applications closed 5 September. **[doc]**

The panel is the real filter. The repo gets you in the room; **the architecture conversation decides
it**. Which is why this brief exists.

---

## 2. The domain is four different problems wearing one coat

The track name suggests one pipeline. It is not. These four leak surfaces have genuinely different
physics — different signals, different legal envelopes, different unit economics, different
definitions of "recovered." Conflating them is the most common way to build something shallow.

### 2.1 Payment failure (one-time, at checkout)

- **Signal:** `payment.failed` webhook, with `error_code` / `error_reason` / `error_source` /
  `error_step`.
- **Window:** minutes. Customer intent is *hot* and decays fast.
- **Right move:** in-session — switch method, switch gateway, re-prompt. Out-of-session — a payment
  link within minutes, not days.
- **"Recovered" means:** a new successful payment on the same `order_id` / cart.
- **Legal envelope:** light. This is a transactional message to someone mid-purchase.

### 2.2 Checkout abandonment (no payment attempted)

- **Signal:** absence, not an event. Order created, never paid. You have to *infer* abandonment from
  a timer, which means the detector has a false-positive cost (people who were going to come back).
- **Window:** hours to ~24h.
- **Right move:** one nudge with a resume link. Prefilled. Possibly with a UPI intent link.
- **"Recovered" means:** conversion attributable to the nudge — **and this is where a holdout is
  non-negotiable**, because a big share of abandoners return on their own.
- **Legal envelope:** heavier. This is arguably marketing. Consent + template category matter.

### 2.3 Subscription / mandate failure (recurring)

- **Signal:** subscription state → `pending`, then `halted`. Invoice stuck in `issued`. **[doc]**
- **Window:** days to weeks. Intent is *warm but ambient* — the customer isn't thinking about you.
- **Right move:** decline-code-conditional retry timing + instrument repair (new card, new mandate)
  + notification. This is classic dunning, and it's the surface with the most published prior art.
- **"Recovered" means:** invoice paid, subscription returns to `active`.
- **Legal envelope:** heaviest on the *debit* side — RBI e-mandate framework governs when you may
  even attempt.

### 2.4 B2B receivables (invoice overdue)

- **Signal:** invoice due date passed, no matching credit.
- **Window:** weeks to months.
- **Right move:** it's a *relationship and process* problem, not an instrument problem. The money
  usually exists. What's missing is an approval, a PO match, a GST detail, or a person. Interventions
  are: reminder → escalation up the AP chain → promise-to-pay capture → payment-plan offer →
  dispute/exception routing.
- **"Recovered" means:** cash received and **reconciled to the right invoice** (which is its own
  hard problem — see Smart Collect below).
- **Legal envelope:** RBI Fair Practices Code territory once it becomes "collections." Calling
  hours, no harassment, agent identification.

> **Interview line:** "I deliberately didn't build one generic pipeline across all four. The
> detection signal, the intent decay curve, the legal envelope, and the definition of 'recovered'
> are different in each. I built a shared *spine* — case ledger, policy engine, action executor,
> audit log — and one deep vertical. Making the other three work is adding detectors and playbooks,
> not rewriting the spine."

---

## 3. The Razorpay substrate (know these cold)

### 3.1 Money representation

Razorpay amounts are **integer minor units** — paise for INR. `amount: 50000` is ₹500.00.
Never a float, anywhere, ever — not in your DB, not in your JSON, not in your aggregation.
Use `Decimal` or `int` paise. There is a well-documented class of Razorpay webhook signature
mismatches caused purely by float reserialization of the body. **[press]**

### 3.2 The error taxonomy — this is the heart of root-cause analysis

Every Razorpay error carries four fields that answer *who / where / why*: **[doc]**

- `code` — e.g. `BAD_REQUEST_ERROR`, `GATEWAY_ERROR`
- `source` — **who**: `customer`, `bank`, `gateway`, `business`, `issuer`, `network`, `internal`
- `step` — **where**: `payment_initiation`, `payment_authentication`, `payment_authorization`,
  `payment_response`
- `reason` — **why**: the machine-readable string you actually branch on
- `description` — human text (never parse this)

**Card errors** — and critically, which are retryable: **[doc]**

| `reason` | Cause | Retryable? | Correct intervention |
|---|---|---|---|
| `insufficient_funds` | low balance | **soft** | retry timed to cash-in, not fixed delay |
| `payment_timed_out` | >~10 min | **soft** | immediate re-prompt |
| `payment_cancelled` | customer backed out | **soft** | re-engage, don't hammer |
| `authentication_failed` | wrong OTP / closed browser | **soft** | re-prompt, native OTP |
| `incorrect_cvv` | wrong CVV | **soft** | re-prompt / CVV-less flow |
| `bank_downtime` | issuer down | **downtime** | wait for resolve, then retry |
| `bank_technical_error` | issuer tech fail | **downtime** | wait for resolve |
| `gateway_technical_error` | partner bank/PG | **downtime** | reroute to another terminal |
| `card_declined` / `payment_failed` | issuer decline | **soft** | alt instrument |
| `payment_risk_check_failed` | issuer suspects fraud | **soft-but-careful** | alt instrument; do NOT hammer |
| `transaction_limit_exceeded` | daily cap hit | **soft** | retry next day / alt instrument |
| `card_expired` | expired | **HARD** | instrument repair only |
| `card_not_enrolled` | not enabled online | **HARD** | customer must enable |
| `card_disabled_for_online_payments` | disabled | **HARD** | customer must enable |
| `debit_instrument_inactive` | not activated | **HARD** | customer must enable |
| `debit_instrument_blocked` | blocked by cust/bank | **HARD** | alt instrument only |

**UPI errors:** **[doc]**

| `reason` | Class | Note |
|---|---|---|
| `insufficient_funds` | soft | same as cards |
| `payment_collect_request_expired` | soft | collect request TTL ~10 min |
| `payment_timed_out` | soft | |
| `payment_cancelled` | soft | customer declined in PSP app |
| `payment_declined` | soft | debit failed |
| `bank_technical_error` | downtime | UPI provider downtime |
| `gateway_technical_error` | downtime | |
| `credit_failed` | downtime-ish | beneficiary-side |
| `invalid_vpa` | **HARD** | not a valid UPI user |
| `vpa_resolution_failed` | **HARD** | can't resolve the handle |
| customer bank account mismatch | **HARD (mandate)** | different account than at registration |

> **The three-class model is the analytical core of the whole project:**
> **HARD** → retrying is pure waste and, on cards, may be a network rules violation. The only valid
> move is *instrument repair* (get a new card / new VPA / new mandate).
> **SOFT** → retry is valid; the entire question is **when**.
> **DOWNTIME** → not the customer's problem at all. Retrying now is worse than useless; it burns an
> attempt against your caps and teaches the issuer you're noisy. Wait for the resolve signal.
>
> Getting a submission to distinguish these three, and to act differently on each, already puts it
> ahead of naive fixed-schedule dunning.

### 3.3 Webhooks

Events you care about: **[doc]**

- `payment.failed`, `payment.authorized`, `payment.captured`, `order.paid`
- `payment.downtime.started`, `payment.downtime.updated`, `payment.downtime.resolved`
- Subscription events: `subscription.charged`, `subscription.pending`, `subscription.halted`,
  `subscription.cancelled` **[verify]** — check exact names in test mode
- Invoice / payment-link events: `invoice.paid`, `payment_link.paid` **[verify]**
- Virtual account: `virtual_account.credited` **[verify]**

**Security & correctness facts to state in the interview:**

1. Signature is `X-Razorpay-Signature` = **HMAC-SHA256 hex over the raw request body**, keyed with
   the *webhook secret* (which is distinct from your API key secret, and distinct per test/live
   mode). Verify with a **constant-time compare** over the **raw bytes** — if you parse-then-
   reserialize before hashing, you will get intermittent mismatches. **[doc]/[press]**
2. **Webhooks are at-least-once and out-of-order.** The payload is "a snapshot of the entity when
   the event occurred" — not current state. So: dedupe on event id, and make every handler
   idempotent and order-tolerant (compare entity state, don't assume sequence). **[doc]**
3. **`payment.failed` followed by `payment.captured` on the same order is normal**, especially UPI,
   because the customer retries inside the same session. **[doc]**
   → *This is a trap worth calling out unprompted in the interview:* a naive recovery agent fires an
   intervention on `payment.failed` and ends up nagging a customer who already paid 30 seconds
   later. The fix is a **settle delay** — hold the case in a `cooling` state for N minutes and
   re-check payment status before acting. It costs you nothing and it is exactly the kind of
   real-world detail that signals you've actually handled payments.

### 3.4 Subscriptions & the gap the agent fills

Razorpay's built-in dunning for card subscriptions: charge fails → subscription goes `pending` →
auto-retry at **T+1, T+2, T+3** → then `halted`. Invoices keep generating on the billing cycle but
auto-charge stops. Invoices sit in `issued` and **can be manually charged via API** as long as they
remain `issued`. Recovery from `halted` requires a new authenticated instrument or a successful
manual charge on an old unpaid invoice. **[doc]**

States: `created → authenticated → active → pending → halted`, plus `paused`, `cancelled` (terminal,
cannot restart), `expired`, `completed`. **[doc]**

> **This is the product thesis.** Razorpay's default is a *fixed, decline-blind, three-shot* schedule.
> That is a reasonable platform default and a bad merchant-specific policy. The agent's job is
> everything the default leaves on the table:
> - **when** to attempt (decline-code- and cash-cycle-conditional, not T+1/2/3)
> - **which instrument** to attempt on
> - **whether to attempt at all** (hard decline → don't)
> - **what to say**, on which channel, in which language
> - **when to stop**
> - and after `halted`, the entire manual-charge-on-`issued`-invoice space, which is untouched by
>   the default.
>
> Say this in the interview. It shows you read the platform docs and found the seam.

### 3.5 Other Razorpay surfaces worth naming

- **Payment Links API** — `POST /payment_links`. Fields: `amount`, `currency`, `description`,
  `customer{name,email,contact}`, `notify{sms,email}`, `reminder_enable`, `callback_url`,
  `expire_by`, `reference_id`, `notes{}`, plus a UPI-link variant. Supports update, cancel, and
  resend-notification. `reference_id` + `notes` are your idempotency and attribution hooks. **[doc]**
- **Smart Collect** — virtual bank accounts and virtual UPI IDs per customer, so inbound
  NEFT/RTGS/IMPS/UPI **auto-reconciles** to the payer. This is the correct answer to "how do you
  know *which* overdue invoice the money that just landed pays off." For B2B receivables it's the
  difference between a demo and a system. **[doc]**
- **Optimizer** — AI/ML payment router across gateways; auto-fails-over when a priority-1 gateway's
  success rate drops; reportedly creates ~20-minute synthetic downtimes for degraded gateways and
  reroutes. Cited ~10% SR uplift on 150+ parameters. **[press]**
  → Relevant because "payment degradation → root cause → recovery action" (a listed example
  direction) is *literally routing*. Know that Razorpay already has this, and position your agent as
  operating at a layer above it (merchant-level revenue cases), not competing with it.
- **Magic Checkout** — one-click checkout, auto-prefill, COD risk-tiering, RTO protection, and
  WhatsApp-native abandoned-cart payment links. Cited ~14% conversion uplift. **[press]**
- **Payment Downtime API** — `GET /payments/downtimes`. Entity has `id`, `method`, `begin`, `end`,
  `status` (`started`/`resolved`), `scheduled`, `severity` (`high`/`medium`/`low`),
  `instrument{issuer|bank|psp}`. Severity semantics: high = issuer/bank/network down; medium =
  elevated declines / low SR; low = unknown cause, minimal impact. **[doc]**

### 3.6 Test mode — what you can actually demo

- Separate test API keys and separate webhook secret. Test payments fire the same webhooks as live.
  **[doc]**
- UPI: `success@razorpay` → instant success; `failure@razorpay` → instant decline. This is your
  lever for generating realistic failure webhooks. **[doc]**
- Test cards by BIN for different card types; any future expiry, any CVV. **[doc]**
- **Constraint to plan around:** test mode will not hand you a rich, adversarial distribution of
  decline reasons, nor months of history. So you will need a **synthetic-but-honest generator** for
  the batch, and you should *say so plainly* rather than implying the numbers are live. Track 04's
  bar explicitly blesses synthetic data ("50+ record batches of synthetic data"), so this is an
  accepted convention in this buildathon — but label it. Honesty about your data is itself scored.

---

## 4. The compliance envelope (this is the "compliant escalation" half of the bar)

Most submissions will treat compliance as a sentence in the README. Treat it as **executable code**:
a policy engine that can *refuse* an action and log why. That single design choice is probably the
highest-leverage differentiator available in this track.

### 4.1 RBI Digital Payments — E-mandate Framework, 2026

Consolidates the earlier recurring-payments circulars across cards, PPIs, and UPI. **[doc]/[press]**

- **First** transaction under a mandate requires **AFA**. Subsequent recurring debits may skip AFA
  **up to ₹15,000** per transaction.
- Raised ceiling of **₹1,00,000** without AFA for specific categories: **insurance premiums, mutual
  fund subscriptions, credit card bill payments**.
- Above the applicable threshold → AFA is mandatory on that debit.
- **Pre-debit notification at least 24 hours before the debit**, and it must contain: merchant name,
  amount, date & time of debit, e-mandate reference number, and reason for debit.
- The customer must get an **opt-out for that specific debit** from the notification.

> **Design consequence, and a great interview answer:** your agent **cannot** simply "retry now" on a
> mandate. A recovery attempt on a mandate is a *scheduled* action with a mandatory ≥24h notice
> ahead of it. So the planner has to reason over a timeline with a hard lead-time constraint, and it
> has to handle the case where the customer opts out of the specific debit — which is a **stopping
> signal**, not a failure to retry through. Encode ₹15,000 / ₹1,00,000 as policy constants with the
> category carve-out, and have the engine reject an over-threshold no-AFA attempt.

### 4.2 RBI Fair Practices Code — recovery conduct

Applies to lenders/NBFCs and their agents; the *norms* are the reference standard for any collections
behaviour in India, and a panel will expect you to know them. **[press, from FPC summaries]**

- **Contact only between 08:00 and 19:00 IST.** Applies to calls, SMS, WhatsApp, email.
- No abusive language, threats, humiliation, or contacting family/references to pressure.
- No workplace visits without prior notice.
- Agents must identify themselves and carry authorisation.
- **The principal is liable for the agent's conduct**, employee or third-party contractor alike.

> **Design consequence:** an outreach scheduler that is **timezone- and quiet-hours-aware**, with a
> hard gate, plus a tone/content guard on anything an LLM generates. "The principal is liable for the
> agent's conduct" is a sentence worth quoting — an autonomous AI collections agent is *exactly* the
> outsourced agent that doctrine contemplates. That framing lands well.

### 4.3 TRAI TCCCPR / DLT — you cannot send free-form messages

**[press, consistent across sources]**

- Every sender registers as a **Principal Entity** on a DLT platform, registers **headers (sender
  IDs)** and **content templates**.
- **Templates are pre-approved. Free-form promotional copy cannot be sent.** Variable slots only.
- **Consent templates** are separately registered, with a Digital Consent Acquisition (OTP-verified)
  flow recorded on DLT.
- Transactional/service vs promotional classification determines DND applicability.

> **Design consequence, and the single sharpest point in the whole project:** the naive
> "AI writes a persuasive dunning message" demo is **illegal in India for SMS**. So invert the
> architecture: **the LLM does not write copy. The LLM selects a registered `template_id` and emits
> a variable binding**, which is then schema-validated and re-checked against the ledger.
>
> This is the answer to "where does the LLM add value and where is it dangerous," and it makes the
> compliance constraint *improve* the architecture instead of limiting it. It also kills the
> hallucinated-amount failure mode by construction: the model never emits a number, it emits a key.

### 4.4 WhatsApp Business Platform

**[press]**

- Categories: **Marketing**, **Utility**, **Authentication** (billed per delivered template), and
  **Service** (free, inside the 24-hour customer-initiated window).
- Per-message billing since 1 July 2025. India rates ≈ ₹0.88 marketing / ₹0.16 utility / ₹0.13 auth.
- Utility = transactional, tied to a customer action. Marketing = business-initiated promotion.
- Opt-in required; templates pre-approved.

> **Design consequence:** channel choice has real unit economics — a marketing template is ~5.5× a
> utility template. A recovery agent should be **cost-aware**: if expected recovery is ₹120, a ₹0.88
> WhatsApp + a ₹4 voice call is a bad plan. Put **expected value = P(recover) × amount − cost of
> action** in the planner and you have a genuinely defensible reason for every escalation step.
> Also: a failed-payment nudge is *Utility*; an abandoned-cart nudge is arguably *Marketing*. That
> distinction changes both cost and consent requirements, and noticing it is a strong signal.

### 4.5 DPDP Act 2023 + DPDP Rules 2025

**[press]** Notified 13 Nov 2025; phased obligations, Consent Manager framework operational
13 Nov 2026, full effect ~mid-May 2027.

- Consent must be **free, specific, informed, unconditional, unambiguous**, per purpose.
- Standalone plain-language notice, available in English + the 22 scheduled languages.
- Data-principal rights: access, correction, erasure, grievance redressal.
- Purpose limitation + storage limitation.

> **Design consequence:** a `consent` table with purpose scoping and timestamps, PII minimisation in
> anything sent to an LLM (hash/tokenise identifiers — this also satisfies the "never send full PII
> into a prompt" rule), and a documented retention policy. Cheap to implement, disproportionately
> credible.

### 4.6 Card network reattempt rules

**[press]** Mostly relevant to card acquiring; India recurring cards additionally sit under the RBI
e-mandate framework.

- Visa: broadly **≤15 reattempts of a single declined transaction in 30 days**; excess incurs a
  per-attempt fee (~$0.10, "Excessive Reattempts"). Visa's **Decline Category 1 = never retry**
  (lost/stolen/closed/never existed). Categories 2–4 are retryable soft declines.
- Mastercard: stricter, **≤10 in 30 days** for declined transactions with the relevant MAC values.

> **Design consequence:** attempt caps aren't just politeness, they're **rules compliance with a
> literal price**. Model them as a **budget per (customer, instrument, 30-day window)** and have the
> policy engine decrement and refuse. And note the alignment: Visa's "Category 1 — never retry" is
> exactly your HARD class. Same idea, independently arrived at. Good thing to point out.

### 4.7 Compliance envelope, condensed

Every outbound action must clear **all** of these before it executes:

```
1.  consent      — purpose-scoped consent on record for this channel?          (DPDP)
2.  quiet hours  — 08:00–19:00 IST at the recipient's locale?                  (RBI FPC)
3.  template     — DLT-registered template_id, variables only?                 (TCCCPR)
4.  channel cost — EV(action) > cost(action)?                                  (economics)
5.  attempt cap  — under per-customer, per-instrument, 30-day budget?          (Visa/MC)
6.  mandate      — ≥24h pre-debit notice sent? amount under AFA threshold?     (RBI e-mandate)
7.  suppression  — no active dispute / chargeback / promise-to-pay / DNC?      (own policy)
8.  frequency    — cooldown since last contact on this case satisfied?         (own policy)
```

Eight gates. Deterministic. Logged with the reason on refusal. **That list, implemented, is the
project's moat.**

---

## 5. Measuring "money recovered" honestly

This is where the track is won or lost, and where almost every hackathon submission is quietly
dishonest.

### 5.1 The problem: self-cure

A large fraction of failed payments and abandoned carts recover **with no intervention at all**.
The customer tops up their account and retries. They come back to the cart that evening. If you
count every post-intervention payment as "recovered by the agent," your headline number is mostly
other people's work.

Published benchmarks for context **[press, vendor-sourced — directional only]**: median
failed-payment recovery ~47.6%; best-in-class programs 70–85%; a plain Day 1/3/5/7 schedule with
*zero* customer communication reportedly recovers ~58%. Involuntary churn is up to ~30% of total
churn. Note what that third figure implies — a large baseline exists before any messaging.

### 5.2 The fix: a randomised holdout

Assign each case, at detection time, to `treatment` or `control` (suggest 85/15), **stratified** by
risk band, amount bucket, and failure class so the arms are comparable. Control gets nothing beyond
the platform default. Then:

```
recovery_rate(arm)  = cases_recovered(arm) / cases(arm)
lift                = recovery_rate(treatment) − recovery_rate(control)
incremental_₹       = lift × cases(treatment) × mean_recovered_amount
cost                = Σ action costs (messages, calls, gateway fees)
ROI                 = (incremental_₹ − cost) / cost
```

Report **both** gross recovered and **incremental** recovered. Report the confidence interval, or at
minimum say "N is small, this is indicative not significant." With N=200 and a 10-point lift you do
not have significance, and **saying so is worth more than the number**.

### 5.3 Attribution window

Define it explicitly and defend it: a payment counts as attributed to an action only if it lands
within **T hours** of that action (say 72h for messaging, 24h for a link with an expiry) and no
other action intervened. Otherwise it's `organic`. Write the rule down; arbitrary-but-stated beats
generous-and-unstated.

### 5.4 The honest exception list

Track 04's bar demands one explicitly and the same instinct applies here. Ship a table of:
cases the agent refused to act on and why; cases where an action failed to send; cases where the
customer opted out; cases the policy engine blocked; hard-decline cases where recovery was
*correctly* judged impossible. **Volunteer the failures.** In this rubric that reads as rigour, not
weakness.

### 5.5 Metrics to put on the dashboard

- ₹ at risk detected, ₹ recovered (gross), **₹ recovered (incremental)**
- recovery rate: treatment vs control, with the delta
- recovery rate **by failure class** (hard / soft / downtime) — proves the taxonomy earns its keep
- mean time-to-recovery
- actions taken per recovered case, and **₹ cost per ₹ recovered**
- policy blocks, by gate — proves the compliance engine is live, not decorative
- false-positive rate on abandonment detection
- stopping-rule firings, by rule

---

## 6. Where the LLM belongs — and where it must not go

Expect this question in the panel, possibly phrased as "why is this an AI project and not a cron job
with a rules table?" Have a crisp answer, and the honest one is *both*.

**LLM does NOT decide:**
- whether to retry a payment, or when → deterministic policy + (optionally) a small supervised model
- any amount, ever → amounts come from the ledger
- whether an action is compliant → policy engine
- whether a case is closed → reconciliation against payments

**LLM DOES:**
1. **Root-cause synthesis.** Fuse heterogeneous signals — error `reason`/`source`/`step`, downtime
   entities, issuer-level SR trend, customer history, method — into a *ranked causal hypothesis with
   a confidence and an evidence list*. This is genuinely hard to express as rules and is the most
   defensible LLM use in the project.
2. **Intervention selection from a bounded menu.** Given the diagnosis and the policy-permitted
   action set, choose the next action and justify it. Constrained decoding over an enum — the model
   picks from actions the policy engine has *already* declared legal, so it cannot choose an illegal
   one. This is the "bounded" in "bounded recovery workflow."
3. **Template + variable binding.** Choose the DLT-registered `template_id` and fill variables.
   Schema-validated. Never free text on regulated channels.
4. **Inbound understanding.** Parse free-text replies ("will pay by Friday after our AP run") into a
   structured `PromiseToPay{amount_paise, promised_date, confidence, verbatim}`. This is real NLU
   value and directly enables the listed "Promise-to-pay tracker" direction.
5. **Hinglish conversation.** Code-mixed Hindi-English is genuinely beyond templates, and it's a
   listed example direction. If you do voice, this is where the model earns it.
6. **Merchant-facing narrative.** "Here's why ₹4.2L was at risk this week and what I did about it."

**The framing that wins the argument:**

> "The LLM is a **planner and a translator**, never an **executor** and never an **authority**. It
> proposes; a deterministic policy engine disposes. Every model output is schema-validated before
> it touches money, and every money-moving action goes through the same eight gates whether a human,
> a rule, or the model proposed it. The audit log records the proposal, the gate results, and the
> outcome — so I can replay any decision and tell you exactly why it happened."

And the corollary they'll respect: *"Roughly 70% of the recovery lift here is from decline-code-
conditional retry timing, which is rules. The model's contribution is concentrated in diagnosis,
inbound parsing, and message selection. I measured them separately."* Ablation beats assertion.

Non-negotiables per Razorpay's own framing ("every money action explainable, bounded and gated") and
standard practice: every LLM call gets a **timeout, token cap, bounded retry, cost tag, and a
deterministic fallback path**. If the model is down, the system degrades to the rules and keeps
running. Say that out loud — it's the difference between a demo and a system.

---

## 7. Anticipated panel questions, with the shape of a good answer

| Question | The answer they want |
|---|---|
| "How do you know that money wouldn't have come anyway?" | Randomised stratified holdout; report incremental ₹ and lift, not gross; state N and significance honestly. |
| "What's your retry policy and why?" | Three-class decline taxonomy. Hard → never retry, repair instrument. Downtime → gate on `payment.downtime.resolved`, not a timer. Soft → time to cash availability + attempt budget. |
| "Why not just retry T+1/2/3 like Razorpay does?" | That's a good *platform* default and a bad *merchant* policy — it's decline-blind. Show recovery rate by failure class to prove conditioning helps. |
| "What stops it from spamming a customer?" | Eight gates, all deterministic, all logged. Quiet hours, cooldowns, attempt budgets, suppression list, opt-out honoured immediately. |
| "What happens when the LLM hallucinates?" | It structurally cannot emit an amount — it emits `template_id` + variable keys, schema-validated, re-checked against the ledger. Worst case is a suboptimal but legal action. |
| "Webhook arrives twice / out of order?" | Dedupe on event id; handlers idempotent and state-comparing, not sequence-assuming; payload is a snapshot, so re-fetch entity state before acting. |
| "A customer pays right after the failure — does it still nag them?" | No — `cooling` state with a settle delay, re-check payment status before acting. `payment.failed` → `payment.captured` on one order is documented-normal on UPI. |
| "How does the agent stop?" | Terminal states: recovered, hard-uncollectible, opted-out, disputed, promise-pending, budget-exhausted, max-escalation-reached. Every case reaches one. |
| "Is this legal?" | RBI e-mandate (24h pre-debit notice, AFA thresholds), RBI FPC (08:00–19:00, conduct), TCCCPR/DLT (registered templates only), DPDP (purpose-scoped consent), WhatsApp opt-in + category. All encoded as gates, not prose. |
| "What breaks at 10,000 cases/day?" | Queue-per-case, idempotency keys, rate budgets per issuer/channel, backpressure, and the LLM on the *diagnosis* path only — not in the hot webhook path. |
| "What did you fake?" | Say it plainly: synthetic batch with a stated generator and distribution; test-mode Razorpay APIs; simulated messaging with a logged outbox rather than real DLT sends. Then say what would change in production. |
| "What's the weakest part?" | Have a real answer ready. Small N, self-cure baseline estimated not measured at scale, no real DLT registration, and the diagnosis model unevaluated against ground-truth labels — because there aren't any. |

---

## 8. Open decisions that shape the build

1. **Which leak surface to go deep on** — subscription/mandate dunning has the richest prior art and
   the cleanest measurement; B2B receivables has the most interesting agentic surface (promise-to-pay,
   negotiation, escalation ladders) and the best story for Smart Collect reconciliation; payment-failure
   recovery is the most "Razorpay-native." Going deep on one and shallow-but-real on a second is
   likely the right shape.
2. **Real Razorpay test-mode integration vs pure simulation** — real test-mode calls are far more
   credible in a panel and the effort is modest. Strong lean toward real.
3. **Voice or no voice** — "Hinglish voice recovery" is a listed direction and demos spectacularly,
   but it's a large build and a compliance minefield (calling hours, consent, recording, disclosure
   that it's an AI). Possibly a scoped bonus, not the core.
4. **Batch size and how synthetic data is generated** — 50 is the floor implied by Track 04; more is
   better for the holdout to mean anything. The generator's realism is itself a defensible artifact.
5. **Merchant-facing surface** — a dashboard is how "measured money recovered" becomes visible. How
   much frontend is worth the time?

---

## Sources

- [Razorpay AI Buildathon](https://razorpay.com/buildathon/)
- [Razorpay — About Errors](https://razorpay.com/docs/errors/) · [Card Error Codes](https://razorpay.com/docs/errors/payments/cards/) · [UPI Error Codes](https://razorpay.com/docs/errors/payments/upi/)
- [Razorpay — Payments Webhook Events](https://razorpay.com/docs/webhooks/payloads/payments/) · [Validate and Test Webhooks](https://razorpay.com/docs/webhooks/validate-test/)
- [Razorpay — Subscription States](https://razorpay.com/docs/payments/subscriptions/states/) · [Payment Retries](https://razorpay.com/docs/payments/subscriptions/payment-retries/)
- [Razorpay — Payment Downtime API](https://razorpay.com/docs/api/payments/downtime/) · [Downtime Entity](https://razorpay.com/docs/api/payments/downtime/entity/)
- [Razorpay — Payment Links API](https://razorpay.com/docs/api/payments/payment-links/) · [Smart Collect](https://razorpay.com/docs/payments/smart-collect/) · [Optimizer](https://razorpay.com/docs/payments/optimizer/) · [Magic Checkout](https://razorpay.com/magic/)
- [Razorpay — Test UPI IDs](https://razorpay.com/docs/payments/payments/test-upi-details/) · [Test and Live Modes](https://razorpay.com/docs/payments/dashboard/test-live-modes/)
- [Razorpay — Rainy Day Kit](https://razorpay.com/docs/payments/payment-gateway/rainy-day/)
- [RBI Digital Payments E-mandate Framework 2026 — summary](https://taxguru.in/rbi/rbi-issues-consolidated-directions-digital-payments-e-mandate-framework-2026.html) · [analysis](https://conventuslaw.com/report/rbis-digital-payments-e-mandate-framework-2026-consolidated-directions-for-recurring-digital-transactions/)
- [RBI Fair Practices Code — recovery agent guidelines summary](https://freed.care/blog/rbi-guidelines-recovery-agents)
- [TRAI TCCCPR Regulation (Feb 2025 amendment, PDF)](https://www.trai.gov.in/sites/default/files/2025-02/Regulation_12022025.pdf) · [DLT compliance guide](https://www.messagecentral.com/en-in/sms-guideline/india)
- [DPDP Rules 2025 — overview](https://securiti.ai/india-digital-personal-data-protection-act-dpdpa-rules/)
- [Visa excessive reattempts rule](https://www.payway.com/blog/understanding-visas-excessive-reattempts-rule-penalties-decline-codes-how-to-stay-compliant) · [Visa/Mastercard retry rules](https://www.slickerhq.com/resources/blog/visa-mastercard-payment-retry-rules)
- [WhatsApp Business pricing categories 2026](https://blueticks.co/blog/whatsapp-business-api-pricing-2026)
- [Failed-payment recovery benchmarks](https://www.slickerhq.com/resources/blog/2025-failed-payment-recovery-benchmarks-saas-median-47-percent)
- [NPCI UPI ecosystem statistics](https://www.npci.org.in/what-we-do/upi/upi-ecosystem-statistics)
