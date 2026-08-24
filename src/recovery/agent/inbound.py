"""Reading what a customer actually wrote.

R2 measured the model against deterministic rules on *timing* and the model
lost decisively. That result stands, and it is why the model does not control
retry scheduling. This module tests the opposite claim on the one capability
rules genuinely lack: turning a sentence a person typed into facts the policy
engine can act on.

No rule turns

    "can't pay today, salary comes Friday, please stop retrying"

into a promise dated to Friday *and* a suppression flag. Keyword matching gets
part of it and breaks on negation, on Hinglish, and on messages whose most
salient keyword points the wrong way.

The output is deliberately shaped like every other model output in this system:

* a closed enum of intents, with ``UNCLEAR`` available so the model can decline
  rather than guess confidently;
* **no amount field**, exactly as in the action schema -- a figure a customer
  claims is not a figure the ledger accepts;
* a ``verbatim`` span, so every reading points at the words that justify it and
  a reviewer can check the extraction without re-reading the whole message.

What the model produces is not an action. It is a set of facts that land in the
existing :class:`~recovery.policy.gates.PolicyContext` -- ``promise_to_pay_until``,
``dispute_open``, ``do_not_contact`` -- and are then gated exactly like anything
else. The model never gains authority by being right about language.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any

MAX_MESSAGE_CHARS = 2000
MAX_VERBATIM_CHARS = 200


class InboundIntent(StrEnum):
    """What the customer is telling us. Closed set."""

    PROMISE_TO_PAY = "promise_to_pay"
    DISPUTE_ALREADY_PAID = "dispute_already_paid"
    REQUEST_STOP_RETRIES = "request_stop_retries"
    PAYMENT_DATE_CHANGE = "payment_date_change"
    PAYMENT_METHOD_CHANGE = "payment_method_change"
    GENERAL_QUESTION = "general_question"
    UNCLEAR = "unclear"
    """Available on purpose. Forcing a choice on ambiguous text produces
    confident wrong answers, which are worse than an admitted non-answer
    because the policy engine acts on them."""


# Intents that must suppress further collection activity until resolved.
SUPPRESSING_INTENTS: frozenset[InboundIntent] = frozenset(
    {
        InboundIntent.DISPUTE_ALREADY_PAID,
        InboundIntent.REQUEST_STOP_RETRIES,
    }
)


@dataclass(frozen=True, slots=True)
class InboundReading:
    """A structured reading of one customer message."""

    intent: InboundIntent
    promised_date: date | None
    requests_no_retry: bool
    confidence: float
    verbatim: str

    def policy_facts(self, *, now: datetime) -> dict[str, Any]:
        """Facts for the existing PolicyContext. Never an action.

        A promise moves ``promise_to_pay_until``; a dispute or an explicit stop
        request sets the suppression flags the gates already consult. Nothing
        here decides what to do -- it decides what is true.
        """
        facts: dict[str, Any] = {}
        if self.intent is InboundIntent.DISPUTE_ALREADY_PAID:
            facts["dispute_open"] = True
        if self.requests_no_retry or self.intent is InboundIntent.REQUEST_STOP_RETRIES:
            facts["do_not_contact"] = True
        if self.promised_date is not None:
            facts["promise_to_pay_until"] = datetime.combine(self.promised_date, now.timetz())
        return facts


class InvalidReading(Exception):
    """The model returned something outside the contract."""


def reading_schema() -> dict[str, Any]:
    """JSON schema for the extraction. Strict-mode compatible."""
    return {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "enum": [i.value for i in InboundIntent],
                "description": "What the customer means; 'unclear' if ambiguous.",
            },
            "promised_date": {
                "anyOf": [
                    {"type": "string", "description": "ISO date, YYYY-MM-DD"},
                    {"type": "null"},
                ],
                "description": "Date they said they would pay, resolved. Null if none.",
            },
            "requests_no_retry": {
                "type": "boolean",
                "description": "True only if they explicitly ask us to stop.",
            },
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "verbatim": {
                "type": "string",
                "description": "The exact span of their message that justifies this reading.",
            },
        },
        "required": ["intent", "promised_date", "requests_no_retry", "confidence", "verbatim"],
        "additionalProperties": False,
    }


def validate_reading(payload: dict[str, Any]) -> InboundReading:
    """Validate a raw extraction, or raise :class:`InvalidReading`."""
    if not isinstance(payload, dict):
        raise InvalidReading(f"expected an object, got {type(payload).__name__}")

    raw_intent = payload.get("intent")
    if not isinstance(raw_intent, str):
        raise InvalidReading("'intent' must be a string")
    try:
        intent = InboundIntent(raw_intent)
    except ValueError as exc:
        raise InvalidReading(f"'{raw_intent}' is not a known intent") from exc

    raw_date = payload.get("promised_date")
    promised: date | None = None
    if raw_date is not None:
        if not isinstance(raw_date, str):
            raise InvalidReading("'promised_date' must be an ISO date string or null")
        try:
            promised = date.fromisoformat(raw_date[:10])
        except ValueError as exc:
            raise InvalidReading(f"'{raw_date}' is not an ISO date") from exc

    confidence = payload.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, int | float):
        raise InvalidReading("'confidence' must be a number")
    if not 0.0 <= float(confidence) <= 1.0:
        raise InvalidReading(f"'confidence' out of range: {confidence}")

    verbatim = payload.get("verbatim")
    if not isinstance(verbatim, str):
        raise InvalidReading("'verbatim' must be a string")

    return InboundReading(
        intent=intent,
        promised_date=promised,
        requests_no_retry=bool(payload.get("requests_no_retry", False)),
        confidence=float(confidence),
        verbatim=verbatim[:MAX_VERBATIM_CHARS],
    )


# --- the baseline ----------------------------------------------------------
#
# A good-faith keyword matcher, not a strawman. It gets multiple phrasings per
# intent, Hinglish spellings, and a real relative-date parser, because a
# baseline built to lose would manufacture the result this experiment exists to
# test. Where keywords are sufficient, this should score.

_STOP_TERMS = (
    "stop retrying",
    "stop trying",
    "stop retry",
    "don't retry",
    "dont retry",
    "do not retry",
    "no more retries",
    "stop charging",
    "stop the charge",
    "stop debiting",
    "stop deducting",
    "stop the auto debit",
    "stop auto debit",
    "do not charge",
    "don't charge",
    "dont charge",
    "do not deduct",
    "stop calling",
    "stop messaging",
    "stop asking",
    "unsubscribe",
    "cancel my subscription",
    "cancel the subscription",
    "pausing this",
    "band karo",
    "band kar",
    "mat karo",
    "mat kaato",
    "mat kato",
    "cancel kar do",
)
_PAID_TERMS = (
    "already paid",
    "already made the payment",
    "already cleared",
    "payment done",
    "paid already",
    "i have paid",
    "ive paid",
    "i've paid",
    "i paid",
    "was settled",
    "already settled",
    "bill already clear",
    "already clear",
    "paisa bhej diya",
    "kar diya payment",
    "pay kar diya",
    "paid this",
    "double charged",
    "charged twice",
    "taken twice",
    "money was taken",
    "wrongly charged",
    "double paisa",
    "amount looks wrong",
    "charge is wrong",
)
_PROMISE_TERMS = (
    "will pay",
    "i'll pay",
    "ill pay",
    "can pay",
    "pay by",
    "pay on",
    "pay after",
    "will settle",
    "will clear",
    "will transfer",
    "will do it",
    "will be done",
    "going to pay",
    "paying on",
    "payment on",
    "clear it",
    "settle this",
    "give me till",
    "give me time",
    "give me a week",
    "give me a few",
    "need until",
    "need till",
    "thoda time",
    "salary",
    "payday",
    "kal",
    "parso",
    "next week",
    "next month",
    "de dunga",
    "kar dunga",
    "karunga",
    "arrange",
    "dekhta hu",
    "dekhte hai",
)
_METHOD_TERMS = (
    "different card",
    "another card",
    "new card",
    "other card",
    "change card",
    "update card",
    "update my card",
    "card details",
    "add a new",
    "adding a new",
    "use upi",
    "by upi",
    "switch to upi",
    "switch it to",
    "net banking",
    "another account",
    "other bank account",
    "dusra card",
    "naya card",
    "card change",
    "change the card",
    "charge this one",
    "fix my card",
)
_DATE_CHANGE_TERMS = (
    "change the date",
    "change my billing",
    "change billing date",
    "billing date",
    "different date",
    "move the date",
    "move my billing",
    "shift my billing",
    "billing cycle",
    "reschedule",
    "due date move",
    "date badal",
    "date change",
    "payment day",
    "payment date to",
)
_QUESTION_TERMS = ("?", "why", "what is", "how much", "kyun", "kitna", "kaise")

_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
_ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_DAY_MONTH = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+"
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b",
    re.I,
)
_MONTHS = {
    m: i
    for i, m in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}


def _extract_date(text: str, today: date) -> date | None:
    """Resolve an explicit or relative date, as well as regex can."""
    lowered = text.lower()

    if match := _ISO_DATE.search(lowered):
        try:
            return date.fromisoformat(match.group(1))
        except ValueError:
            pass

    if match := _DAY_MONTH.search(lowered):
        day, month = int(match.group(1)), _MONTHS[match.group(2)[:3].lower()]
        year = today.year + (1 if month < today.month else 0)
        try:
            return date(year, month, day)
        except ValueError:
            pass

    if "tomorrow" in lowered or "kal" in lowered:
        return today + timedelta(days=1)
    if "day after" in lowered or "parso" in lowered:
        return today + timedelta(days=2)
    if "next week" in lowered:
        return today + timedelta(days=7)
    if "next month" in lowered:
        return today + timedelta(days=30)

    for name, index in _WEEKDAYS.items():
        if name in lowered:
            ahead = (index - today.weekday()) % 7 or 7
            return today + timedelta(days=ahead)

    if match := re.search(r"\bin (\d{1,2}) days?\b", lowered):
        return today + timedelta(days=int(match.group(1)))
    return None


def keyword_reading(message: str, *, today: date) -> InboundReading:
    """Baseline extraction using keywords and date regexes.

    Deliberately competent. It checks the strongest signals first, handles
    Hinglish spellings, and resolves relative dates -- so a win for the model
    over this is a win over what a careful engineer would actually ship without
    one.
    """
    text = message.lower()[:MAX_MESSAGE_CHARS]

    def hit(terms: tuple[str, ...]) -> str | None:
        return next((t for t in terms if t in text), None)

    stop = hit(_STOP_TERMS)
    paid = hit(_PAID_TERMS)
    promise = hit(_PROMISE_TERMS)
    method = hit(_METHOD_TERMS)
    date_change = hit(_DATE_CHANGE_TERMS)
    promised_date = _extract_date(text, today)

    if paid:
        intent, span = InboundIntent.DISPUTE_ALREADY_PAID, paid
    elif stop:
        intent, span = InboundIntent.REQUEST_STOP_RETRIES, stop
    elif method:
        intent, span = InboundIntent.PAYMENT_METHOD_CHANGE, method
    elif date_change:
        intent, span = InboundIntent.PAYMENT_DATE_CHANGE, date_change
    elif promise:
        intent, span = InboundIntent.PROMISE_TO_PAY, promise
    elif hit(_QUESTION_TERMS) is not None:
        intent, span = InboundIntent.GENERAL_QUESTION, "?"
    else:
        intent, span = InboundIntent.UNCLEAR, ""

    # Keep an extracted date wherever a payment date is plausibly being stated --
    # including a stop request that also names one, which the multi-intent
    # convention says to retain. Discarding it there would handicap the baseline
    # against a rule this codebase documents, and a comparison against a
    # hobbled opponent proves nothing.
    _DATE_BEARING = (
        InboundIntent.PROMISE_TO_PAY,
        InboundIntent.PAYMENT_DATE_CHANGE,
        InboundIntent.REQUEST_STOP_RETRIES,
    )
    if intent not in _DATE_BEARING:
        promised_date = None

    return InboundReading(
        intent=intent,
        promised_date=promised_date,
        requests_no_retry=stop is not None,
        confidence=0.5 if intent is not InboundIntent.UNCLEAR else 0.2,
        verbatim=span,
    )


# --- model extraction ------------------------------------------------------

EXTRACTION_PROMPT = """\
You read one message a customer sent about a failed subscription payment and
report what it means. You do not decide what to do about it -- a deterministic
policy engine does that with the facts you return.

Rules:

- Choose exactly one intent from the enum. Use `unclear` when the message is
  genuinely ambiguous; a confident wrong reading is worse than an admitted
  non-answer, because the system acts on what you return.
- `promised_date` is only for a date they said they would PAY. Resolve relative
  dates ("Friday", "kal", "next week", "in 3 days") against today's date, which
  is given to you. Return null if they named no payment date.
- `requests_no_retry` is true only when they explicitly ask you to stop
  attempting or contacting. Someone who merely says they cannot pay yet has not
  asked you to stop.
- Read negation and contrast carefully. "I already paid last month but not this
  one, I'll clear it Friday" is a promise to pay, not a dispute. "I did not say
  stop" is not a request to stop.
- When a message does both -- promises payment AND asks you to stop attempting --
  report `request_stop_retries` as the intent, keep the promised date, and set
  `requests_no_retry`. The instruction about our behaviour takes the intent slot
  because acting against it is the harm worth avoiding; the date is still a fact
  and is still reported.
- Many customers write in Hinglish. Treat it as ordinary language.
- `verbatim` is the exact span of their message that justifies your reading.

Report only what the message says. Never infer an amount."""


def build_extraction_prompt(message: str, today: date) -> str:
    return (
        f"Today is {today.isoformat()} ({today.strftime('%A')}).\n\n"
        f"Customer message:\n{message[:MAX_MESSAGE_CHARS]}"
    )


class InboundExtractor:
    """Reads customer messages with a model, falling back to keywords.

    Same discipline as the action planner: the model proposes facts, validation
    rejects anything outside the contract, and a deterministic path catches
    every failure. A model outage degrades understanding; it does not stop the
    system.
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.fallbacks = 0
        self.errors: list[str] = []

    def read(self, message: str, *, today: date) -> tuple[InboundReading, bool]:
        """Return (reading, used_model)."""
        reply = self._client.propose(
            system=EXTRACTION_PROMPT,
            prompt=build_extraction_prompt(message, today),
            schema=reading_schema(),
        )
        self.calls += 1
        self.input_tokens += reply.input_tokens
        self.output_tokens += reply.output_tokens

        if not reply.ok or reply.payload is None:
            self.errors.append(reply.error or "unknown")
            self.fallbacks += 1
            return keyword_reading(message, today=today), False
        try:
            return validate_reading(reply.payload), True
        except InvalidReading as exc:
            self.errors.append(f"invalid_reading:{exc}")
            self.fallbacks += 1
            return keyword_reading(message, today=today), False
