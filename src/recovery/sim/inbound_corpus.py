"""Labelled customer messages for the inbound-understanding benchmark.

Reference date: **2026-08-25, a Tuesday.** Relative dates in the labels are
resolved against it.

HOW THIS WAS BUILT, AND WHAT THAT LIMITS
----------------------------------------
The messages and their labels were written by the same author as the system
under test. That is a real weakness and it is stated rather than buried: a
corpus written to suit a model will flatter it. Three things push against that:

* **The keyword baseline is written in good faith**, with several phrasings per
  intent, Hinglish spellings and a working relative-date parser. Where keywords
  suffice, the baseline is meant to score.
* **The mix is deliberately ordinary.** Most entries are plain messages either
  approach should read correctly. If the model only wins on the adversarial
  slice, the aggregate will say so.
* **Every label is defensible from the text alone.** A reader who disagrees with
  one can check it against the message; nothing depends on hidden context.

Categories, so per-slice performance can be read rather than averaged away:

``plain``       unambiguous, keyword-friendly. Both should get these.
``relative``    the date is expressed relatively rather than stated.
``hinglish``    code-mixed Hindi-English, as Indian customers actually write.
``trap``        the most salient keyword points at the wrong intent.
``ambiguous``   genuinely unclear; UNCLEAR is the correct answer.
``multi``       both a promise and a stop request in one message.

THE MULTI-INTENT CONVENTION
---------------------------
A customer who says "I'll pay Friday but stop charging my card daily" has told
us two things. The labelling rule, applied consistently:

    intent            = request_stop_retries  (the operative instruction)
    promised_date     = the date they gave    (still a fact worth keeping)
    requests_no_retry = True

The stop request wins the intent slot because it is an instruction about our
behaviour, and acting against it is the harm worth avoiding. The date survives
regardless, so ``policy_facts`` carries both.

This convention was made explicit in the extraction prompt **after** an initial
run, where the model read such messages as promises. That is post-hoc, and it is
disclosed in docs/RESULTS.md with the before-and-after numbers rather than
quietly folded in. The keyword baseline already encoded the same precedence by
accident of ordering, so the change removes an unfair asymmetry rather than
creating one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from recovery.agent.inbound import InboundIntent

TODAY = date(2026, 8, 25)  # Tuesday


@dataclass(frozen=True, slots=True)
class LabelledMessage:
    """One customer message and what it actually means."""

    text: str
    intent: InboundIntent
    promised_date: date | None
    requests_no_retry: bool
    category: str


Intent = InboundIntent
D = date

CORPUS: tuple[LabelledMessage, ...] = (
    # --- plain: both approaches should handle these -----------------------
    LabelledMessage(
        "I will pay on 2026-08-28", Intent.PROMISE_TO_PAY, D(2026, 8, 28), False, "plain"
    ),
    LabelledMessage(
        "I already paid this invoice last week", Intent.DISPUTE_ALREADY_PAID, None, False, "plain"
    ),
    LabelledMessage(
        "Please stop retrying my card", Intent.REQUEST_STOP_RETRIES, None, True, "plain"
    ),
    LabelledMessage(
        "Can I use a different card instead?", Intent.PAYMENT_METHOD_CHANGE, None, False, "plain"
    ),
    LabelledMessage(
        "Why was I charged this amount?", Intent.GENERAL_QUESTION, None, False, "plain"
    ),
    LabelledMessage("I'll pay by 30 August", Intent.PROMISE_TO_PAY, D(2026, 8, 30), False, "plain"),
    LabelledMessage(
        "Payment done already, please check", Intent.DISPUTE_ALREADY_PAID, None, False, "plain"
    ),
    LabelledMessage("Stop charging my account", Intent.REQUEST_STOP_RETRIES, None, True, "plain"),
    LabelledMessage(
        "I want to change my billing date", Intent.PAYMENT_DATE_CHANGE, None, False, "plain"
    ),
    LabelledMessage("How much do I owe?", Intent.GENERAL_QUESTION, None, False, "plain"),
    LabelledMessage(
        "Update my card please, the old one expired",
        Intent.PAYMENT_METHOD_CHANGE,
        None,
        False,
        "plain",
    ),
    LabelledMessage(
        "I have paid, you charged me twice", Intent.DISPUTE_ALREADY_PAID, None, False, "plain"
    ),
    LabelledMessage(
        "Do not retry, I am cancelling", Intent.REQUEST_STOP_RETRIES, None, True, "plain"
    ),
    LabelledMessage(
        "What is this subscription for?", Intent.GENERAL_QUESTION, None, False, "plain"
    ),
    LabelledMessage(
        "I can pay on 1 September", Intent.PROMISE_TO_PAY, D(2026, 9, 1), False, "plain"
    ),
    # --- relative: the date has to be worked out --------------------------
    LabelledMessage("I'll pay tomorrow", Intent.PROMISE_TO_PAY, D(2026, 8, 26), False, "relative"),
    LabelledMessage(
        "Salary comes Friday, I'll pay then",
        Intent.PROMISE_TO_PAY,
        D(2026, 8, 28),
        False,
        "relative",
    ),
    LabelledMessage(
        "Can pay next week sometime", Intent.PROMISE_TO_PAY, D(2026, 9, 1), False, "relative"
    ),
    LabelledMessage(
        "Give me 3 days please", Intent.PROMISE_TO_PAY, D(2026, 8, 28), False, "relative"
    ),
    LabelledMessage(
        "day after tomorrow I will transfer",
        Intent.PROMISE_TO_PAY,
        D(2026, 8, 27),
        False,
        "relative",
    ),
    LabelledMessage(
        "I get paid on Monday, will clear it",
        Intent.PROMISE_TO_PAY,
        D(2026, 8, 31),
        False,
        "relative",
    ),
    LabelledMessage(
        "next month I can arrange it", Intent.PROMISE_TO_PAY, D(2026, 9, 24), False, "relative"
    ),
    LabelledMessage(
        "in 5 days I'll have the funds", Intent.PROMISE_TO_PAY, D(2026, 8, 30), False, "relative"
    ),
    # --- hinglish: how a lot of Indian customers actually write -----------
    LabelledMessage(
        "kal payment kar dunga", Intent.PROMISE_TO_PAY, D(2026, 8, 26), False, "hinglish"
    ),
    LabelledMessage(
        "maine paisa bhej diya hai already", Intent.DISPUTE_ALREADY_PAID, None, False, "hinglish"
    ),
    LabelledMessage(
        "please retry band karo, main baad me karunga",
        Intent.REQUEST_STOP_RETRIES,
        None,
        True,
        "hinglish",
    ),
    LabelledMessage(
        "salary aane ke baad Friday ko kar dunga",
        Intent.PROMISE_TO_PAY,
        D(2026, 8, 28),
        False,
        "hinglish",
    ),
    LabelledMessage(
        "card change karna hai, dusra card use karunga",
        Intent.PAYMENT_METHOD_CHANGE,
        None,
        False,
        "hinglish",
    ),
    LabelledMessage(
        "ye charge kyun laga hai mujhe?", Intent.GENERAL_QUESTION, None, False, "hinglish"
    ),
    LabelledMessage(
        "parso tak kar dunga payment", Intent.PROMISE_TO_PAY, D(2026, 8, 27), False, "hinglish"
    ),
    LabelledMessage(
        "billing date badal sakte ho kya", Intent.PAYMENT_DATE_CHANGE, None, False, "hinglish"
    ),
    # --- traps: the loudest keyword points the wrong way ------------------
    LabelledMessage(
        "I already paid last month's invoice but not this one, I'll clear this by Friday",
        Intent.PROMISE_TO_PAY,
        D(2026, 8, 28),
        False,
        "trap",
    ),
    LabelledMessage(
        "I did not say stop retrying, I said retry after Friday",
        Intent.PROMISE_TO_PAY,
        D(2026, 8, 28),
        False,
        "trap",
    ),
    LabelledMessage(
        "My friend said he already paid his, but mine is still pending. When is it due?",
        Intent.GENERAL_QUESTION,
        None,
        False,
        "trap",
    ),
    LabelledMessage(
        "I was going to pay tomorrow but now I want to dispute this, I paid in July",
        Intent.DISPUTE_ALREADY_PAID,
        None,
        False,
        "trap",
    ),
    LabelledMessage(
        "Don't stop my subscription, I will pay on 29 August",
        Intent.PROMISE_TO_PAY,
        D(2026, 8, 29),
        False,
        "trap",
    ),
    LabelledMessage(
        "Not asking you to stop, just don't charge the old card, use my new one",
        Intent.PAYMENT_METHOD_CHANGE,
        None,
        False,
        "trap",
    ),
    LabelledMessage(
        "salary next week hai lekin abhi mat try karo, band karo abhi",
        Intent.REQUEST_STOP_RETRIES,
        None,
        True,
        "trap",
    ),
    LabelledMessage(
        "I will pay eventually but stop retrying my card every day",
        Intent.REQUEST_STOP_RETRIES,
        None,
        True,
        "trap",
    ),
    LabelledMessage(
        "Payment done for the annual plan, this monthly charge is wrong",
        Intent.DISPUTE_ALREADY_PAID,
        None,
        False,
        "trap",
    ),
    LabelledMessage(
        "Can you tell me why my card keeps failing? I want to fix it",
        Intent.GENERAL_QUESTION,
        None,
        False,
        "trap",
    ),
    # --- ambiguous: UNCLEAR is the right answer ---------------------------
    LabelledMessage("ok", Intent.UNCLEAR, None, False, "ambiguous"),
    LabelledMessage("hmm let me see", Intent.UNCLEAR, None, False, "ambiguous"),
    LabelledMessage("thanks", Intent.UNCLEAR, None, False, "ambiguous"),
    LabelledMessage("later", Intent.UNCLEAR, None, False, "ambiguous"),
    LabelledMessage("acha theek hai", Intent.UNCLEAR, None, False, "ambiguous"),
    LabelledMessage("...", Intent.UNCLEAR, None, False, "ambiguous"),
    # --- plain, continued -------------------------------------------------
    LabelledMessage(
        "Will settle this on 5 September", Intent.PROMISE_TO_PAY, D(2026, 9, 5), False, "plain"
    ),
    LabelledMessage(
        "I paid this yesterday through the app", Intent.DISPUTE_ALREADY_PAID, None, False, "plain"
    ),
    LabelledMessage(
        "Please do not deduct from this account any more",
        Intent.REQUEST_STOP_RETRIES,
        None,
        True,
        "plain",
    ),
    LabelledMessage(
        "My card is blocked, can I pay by UPI", Intent.PAYMENT_METHOD_CHANGE, None, False, "plain"
    ),
    LabelledMessage(
        "Is this charge monthly or yearly?", Intent.GENERAL_QUESTION, None, False, "plain"
    ),
    LabelledMessage(
        "I'll transfer the money on 2026-09-02",
        Intent.PROMISE_TO_PAY,
        D(2026, 9, 2),
        False,
        "plain",
    ),
    LabelledMessage(
        "This was settled long back, check your records",
        Intent.DISPUTE_ALREADY_PAID,
        None,
        False,
        "plain",
    ),
    LabelledMessage("Unsubscribe me please", Intent.REQUEST_STOP_RETRIES, None, True, "plain"),
    LabelledMessage(
        "Shift my billing to the 10th of each month",
        Intent.PAYMENT_DATE_CHANGE,
        None,
        False,
        "plain",
    ),
    LabelledMessage(
        "Who do I contact about this invoice?", Intent.GENERAL_QUESTION, None, False, "plain"
    ),
    LabelledMessage(
        "Adding a new debit card now", Intent.PAYMENT_METHOD_CHANGE, None, False, "plain"
    ),
    LabelledMessage(
        "Money was taken twice for August", Intent.DISPUTE_ALREADY_PAID, None, False, "plain"
    ),
    LabelledMessage(
        "Payment on 28 August confirmed from my side",
        Intent.PROMISE_TO_PAY,
        D(2026, 8, 28),
        False,
        "plain",
    ),
    LabelledMessage(
        "Cancel my subscription, I do not want it", Intent.REQUEST_STOP_RETRIES, None, True, "plain"
    ),
    LabelledMessage("What happens if I do not pay?", Intent.GENERAL_QUESTION, None, False, "plain"),
    LabelledMessage(
        "Can the due date move to month end?", Intent.PAYMENT_DATE_CHANGE, None, False, "plain"
    ),
    LabelledMessage(
        "I'll clear it on 31 August without fail",
        Intent.PROMISE_TO_PAY,
        D(2026, 8, 31),
        False,
        "plain",
    ),
    LabelledMessage(
        "Switch it to my HDFC card", Intent.PAYMENT_METHOD_CHANGE, None, False, "plain"
    ),
    LabelledMessage(
        "The amount looks wrong, I paid in full", Intent.DISPUTE_ALREADY_PAID, None, False, "plain"
    ),
    LabelledMessage(
        "Stop messaging me about this", Intent.REQUEST_STOP_RETRIES, None, True, "plain"
    ),
    LabelledMessage("Which plan am I on right now?", Intent.GENERAL_QUESTION, None, False, "plain"),
    LabelledMessage(
        "I can pay on 3 September", Intent.PROMISE_TO_PAY, D(2026, 9, 3), False, "plain"
    ),
    LabelledMessage(
        "Use my other bank account from now on", Intent.PAYMENT_METHOD_CHANGE, None, False, "plain"
    ),
    LabelledMessage(
        "I already made the payment on Monday", Intent.DISPUTE_ALREADY_PAID, None, False, "plain"
    ),
    LabelledMessage("Do not retry my card again", Intent.REQUEST_STOP_RETRIES, None, True, "plain"),
    LabelledMessage("Send me the invoice copy", Intent.GENERAL_QUESTION, None, False, "plain"),
    LabelledMessage(
        "Change my payment day to the 15th", Intent.PAYMENT_DATE_CHANGE, None, False, "plain"
    ),
    LabelledMessage(
        "Paying on 2026-08-29 for sure", Intent.PROMISE_TO_PAY, D(2026, 8, 29), False, "plain"
    ),
    LabelledMessage(
        "I want to update my card details", Intent.PAYMENT_METHOD_CHANGE, None, False, "plain"
    ),
    LabelledMessage("Why did the payment fail?", Intent.GENERAL_QUESTION, None, False, "plain"),
    # --- relative, continued ----------------------------------------------
    LabelledMessage(
        "I'll pay on Thursday", Intent.PROMISE_TO_PAY, D(2026, 8, 27), False, "relative"
    ),
    LabelledMessage(
        "Give me till Saturday", Intent.PROMISE_TO_PAY, D(2026, 8, 29), False, "relative"
    ),
    LabelledMessage(
        "Sunday I will do it", Intent.PROMISE_TO_PAY, D(2026, 8, 30), False, "relative"
    ),
    LabelledMessage(
        "in 10 days I can manage it", Intent.PROMISE_TO_PAY, D(2026, 9, 4), False, "relative"
    ),
    LabelledMessage(
        "I get my salary next week, will pay then",
        Intent.PROMISE_TO_PAY,
        D(2026, 9, 1),
        False,
        "relative",
    ),
    LabelledMessage(
        "by wednesday it will be done", Intent.PROMISE_TO_PAY, D(2026, 8, 26), False, "relative"
    ),
    LabelledMessage(
        "in 2 days I will transfer", Intent.PROMISE_TO_PAY, D(2026, 8, 27), False, "relative"
    ),
    LabelledMessage(
        "next month sometime, money is tight",
        Intent.PROMISE_TO_PAY,
        D(2026, 9, 24),
        False,
        "relative",
    ),
    LabelledMessage(
        "I'll pay day after tomorrow", Intent.PROMISE_TO_PAY, D(2026, 8, 27), False, "relative"
    ),
    LabelledMessage("in 7 days please", Intent.PROMISE_TO_PAY, D(2026, 9, 1), False, "relative"),
    LabelledMessage(
        "Tomorrow morning I will pay", Intent.PROMISE_TO_PAY, D(2026, 8, 26), False, "relative"
    ),
    LabelledMessage(
        "Monday is my payday", Intent.PROMISE_TO_PAY, D(2026, 8, 31), False, "relative"
    ),
    LabelledMessage(
        "in 4 days I will have it", Intent.PROMISE_TO_PAY, D(2026, 8, 29), False, "relative"
    ),
    LabelledMessage(
        "Friday evening after work", Intent.PROMISE_TO_PAY, D(2026, 8, 28), False, "relative"
    ),
    LabelledMessage("give me a week", Intent.PROMISE_TO_PAY, D(2026, 9, 1), False, "relative"),
    LabelledMessage(
        "I will pay this Thursday for sure",
        Intent.PROMISE_TO_PAY,
        D(2026, 8, 27),
        False,
        "relative",
    ),
    LabelledMessage("in 3 days time", Intent.PROMISE_TO_PAY, D(2026, 8, 28), False, "relative"),
    LabelledMessage("by next Tuesday", Intent.PROMISE_TO_PAY, D(2026, 9, 1), False, "relative"),
    # --- hinglish, continued ----------------------------------------------
    LabelledMessage(
        "bhai thoda time do, Friday tak kar dunga",
        Intent.PROMISE_TO_PAY,
        D(2026, 8, 28),
        False,
        "hinglish",
    ),
    LabelledMessage(
        "maine to pay kar diya tha, phir se kyun kaat rahe ho",
        Intent.DISPUTE_ALREADY_PAID,
        None,
        False,
        "hinglish",
    ),
    LabelledMessage(
        "abhi paise nahi hai, agle hafte dekhta hu",
        Intent.PROMISE_TO_PAY,
        D(2026, 9, 1),
        False,
        "hinglish",
    ),
    LabelledMessage(
        "mera card band hai, UPI se kar sakta hu kya",
        Intent.PAYMENT_METHOD_CHANGE,
        None,
        False,
        "hinglish",
    ),
    LabelledMessage("ye plan kitne ka hai?", Intent.GENERAL_QUESTION, None, False, "hinglish"),
    LabelledMessage(
        "bar bar mat kaato, main khud kar dunga",
        Intent.REQUEST_STOP_RETRIES,
        None,
        True,
        "hinglish",
    ),
    LabelledMessage(
        "Monday ko salary aayegi tab kar dunga",
        Intent.PROMISE_TO_PAY,
        D(2026, 8, 31),
        False,
        "hinglish",
    ),
    LabelledMessage(
        "subscription cancel kar do please", Intent.REQUEST_STOP_RETRIES, None, True, "hinglish"
    ),
    LabelledMessage(
        "double paisa kat gaya hai mera", Intent.DISPUTE_ALREADY_PAID, None, False, "hinglish"
    ),
    LabelledMessage(
        "date change ho sakti hai kya month end pe",
        Intent.PAYMENT_DATE_CHANGE,
        None,
        False,
        "hinglish",
    ),
    LabelledMessage(
        "parso pakka kar dunga, tension mat lo",
        Intent.PROMISE_TO_PAY,
        D(2026, 8, 27),
        False,
        "hinglish",
    ),
    LabelledMessage(
        "naya card add kar raha hu abhi", Intent.PAYMENT_METHOD_CHANGE, None, False, "hinglish"
    ),
    LabelledMessage(
        "payment fail kyun ho raha hai baar baar", Intent.GENERAL_QUESTION, None, False, "hinglish"
    ),
    LabelledMessage("2 din me kar dunga", Intent.PROMISE_TO_PAY, D(2026, 8, 27), False, "hinglish"),
    LabelledMessage(
        "bill already clear hai mera", Intent.DISPUTE_ALREADY_PAID, None, False, "hinglish"
    ),
    LabelledMessage(
        "agle mahine dekhte hai", Intent.PROMISE_TO_PAY, D(2026, 9, 24), False, "hinglish"
    ),
    LabelledMessage("kitna baaki hai batao", Intent.GENERAL_QUESTION, None, False, "hinglish"),
    LabelledMessage(
        "Saturday tak time chahiye", Intent.PROMISE_TO_PAY, D(2026, 8, 29), False, "hinglish"
    ),
    # --- traps, continued -------------------------------------------------
    LabelledMessage(
        "I am not disputing anything, I just need until Friday",
        Intent.PROMISE_TO_PAY,
        D(2026, 8, 28),
        False,
        "trap",
    ),
    LabelledMessage(
        "Nobody told me to stop, I want to continue, just fix my card",
        Intent.PAYMENT_METHOD_CHANGE,
        None,
        False,
        "trap",
    ),
    LabelledMessage(
        "I paid the joining fee, not this month's charge. What is it for?",
        Intent.GENERAL_QUESTION,
        None,
        False,
        "trap",
    ),
    LabelledMessage(
        "Cancel the old card not the subscription, I'll pay on Thursday",
        Intent.PAYMENT_METHOD_CHANGE,
        None,
        False,
        "trap",
    ),
    LabelledMessage(
        "It is not that I already paid, it is that I cannot pay yet",
        Intent.PROMISE_TO_PAY,
        None,
        False,
        "trap",
    ),
    LabelledMessage(
        "My wife already paid hers. Mine failed. Why?", Intent.GENERAL_QUESTION, None, False, "trap"
    ),
    LabelledMessage(
        "stop asking me the same question, just change my billing date",
        Intent.PAYMENT_DATE_CHANGE,
        None,
        False,
        "trap",
    ),
    LabelledMessage(
        "payment done nahi hua abhi tak, kal karunga",
        Intent.PROMISE_TO_PAY,
        D(2026, 8, 26),
        False,
        "trap",
    ),
    LabelledMessage(
        "Not next week, this Friday I will pay",
        Intent.PROMISE_TO_PAY,
        D(2026, 8, 28),
        False,
        "trap",
    ),
    LabelledMessage(
        "I do not want to cancel, I want to change the card",
        Intent.PAYMENT_METHOD_CHANGE,
        None,
        False,
        "trap",
    ),
    LabelledMessage(
        "Everyone says already paid, but I genuinely have not", Intent.UNCLEAR, None, False, "trap"
    ),
    LabelledMessage(
        "retry karo but Friday ke baad", Intent.PROMISE_TO_PAY, D(2026, 8, 28), False, "trap"
    ),
    LabelledMessage(
        "The email said payment done but my bank shows nothing",
        Intent.GENERAL_QUESTION,
        None,
        False,
        "trap",
    ),
    LabelledMessage(
        "I will not pay until you tell me what this is",
        Intent.GENERAL_QUESTION,
        None,
        False,
        "trap",
    ),
    LabelledMessage(
        "stop charging that card, charge this one instead",
        Intent.PAYMENT_METHOD_CHANGE,
        None,
        False,
        "trap",
    ),
    LabelledMessage(
        "tomorrow is not possible, make it Saturday",
        Intent.PROMISE_TO_PAY,
        D(2026, 8, 29),
        False,
        "trap",
    ),
    LabelledMessage(
        "Already paid? No. Will pay? Yes, on 30 August",
        Intent.PROMISE_TO_PAY,
        D(2026, 8, 30),
        False,
        "trap",
    ),
    LabelledMessage(
        "Do not stop the service, just move my billing date",
        Intent.PAYMENT_DATE_CHANGE,
        None,
        False,
        "trap",
    ),
    # --- multi-intent: promise AND stop request ---------------------------
    LabelledMessage(
        "I'll pay Friday but stop charging my card daily",
        Intent.REQUEST_STOP_RETRIES,
        D(2026, 8, 28),
        True,
        "multi",
    ),
    LabelledMessage(
        "stop retrying, I will pay on 30 August myself",
        Intent.REQUEST_STOP_RETRIES,
        D(2026, 8, 30),
        True,
        "multi",
    ),
    LabelledMessage(
        "please band karo retry, Monday ko main kar dunga",
        Intent.REQUEST_STOP_RETRIES,
        D(2026, 8, 31),
        True,
        "multi",
    ),
    LabelledMessage(
        "Do not retry. I will transfer next week.",
        Intent.REQUEST_STOP_RETRIES,
        D(2026, 9, 1),
        True,
        "multi",
    ),
    LabelledMessage(
        "I can pay tomorrow, but stop deducting automatically",
        Intent.REQUEST_STOP_RETRIES,
        D(2026, 8, 26),
        True,
        "multi",
    ),
    LabelledMessage(
        "stop trying my card, salary comes Thursday",
        Intent.REQUEST_STOP_RETRIES,
        D(2026, 8, 27),
        True,
        "multi",
    ),
    LabelledMessage(
        "give me till Saturday and stop messaging me daily",
        Intent.REQUEST_STOP_RETRIES,
        D(2026, 8, 29),
        True,
        "multi",
    ),
    LabelledMessage(
        "I will settle on 2 September, until then do not charge",
        Intent.REQUEST_STOP_RETRIES,
        D(2026, 9, 2),
        True,
        "multi",
    ),
    LabelledMessage(
        "mat kaato abhi, parso kar dunga",
        Intent.REQUEST_STOP_RETRIES,
        D(2026, 8, 27),
        True,
        "multi",
    ),
    LabelledMessage(
        "Pausing this. I will pay in 5 days on my own.",
        Intent.REQUEST_STOP_RETRIES,
        D(2026, 8, 30),
        True,
        "multi",
    ),
    LabelledMessage(
        "stop the auto debit, I will pay manually on Friday",
        Intent.REQUEST_STOP_RETRIES,
        D(2026, 8, 28),
        True,
        "multi",
    ),
    LabelledMessage(
        "no more retries please, next month I will clear it",
        Intent.REQUEST_STOP_RETRIES,
        D(2026, 9, 24),
        True,
        "multi",
    ),
    # --- ambiguous, continued ---------------------------------------------
    LabelledMessage("k", Intent.UNCLEAR, None, False, "ambiguous"),
    LabelledMessage("noted", Intent.UNCLEAR, None, False, "ambiguous"),
    LabelledMessage("hmm", Intent.UNCLEAR, None, False, "ambiguous"),
    LabelledMessage("dekhta hu", Intent.UNCLEAR, None, False, "ambiguous"),
    LabelledMessage("soon", Intent.UNCLEAR, None, False, "ambiguous"),
    LabelledMessage("??", Intent.UNCLEAR, None, False, "ambiguous"),
    LabelledMessage("theek hai", Intent.UNCLEAR, None, False, "ambiguous"),
    LabelledMessage("call me", Intent.UNCLEAR, None, False, "ambiguous"),
    LabelledMessage("busy right now", Intent.UNCLEAR, None, False, "ambiguous"),
    LabelledMessage("ok fine", Intent.UNCLEAR, None, False, "ambiguous"),
)

CATEGORIES: tuple[str, ...] = (
    "plain",
    "relative",
    "hinglish",
    "trap",
    "multi",
    "ambiguous",
)
