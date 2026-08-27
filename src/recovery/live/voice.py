"""A voice agent that structurally cannot misstate money.

**The model gets ears, not a mouth.**

Recognition is the one capability R3 measured the model as genuinely better at:
+12 points over a competent keyword baseline, McNemar p=0.0019. No rule turns
*"salary aane ke baad Friday ko kar dunga"* into a promise dated to Friday. So
listening is delegated -- and it is delegated to the module that already
carries that evidence, :mod:`recovery.agent.inbound`, unchanged. Voice adds a
transport, not a second brain: there is no new model schema in this file.

Speaking is not delegated. Every word the system says is a fixed utterance
whose slots are filled from the case ledger by :func:`speak`. The model's
output for a turn is an :class:`~recovery.agent.inbound.InboundReading`, which
has no amount field -- so a compromised model, or a caller who talks their way
into one, still cannot make the phone say a wrong rupee figure. There is
nowhere for the figure to travel.

*Rejected:* letting a realtime speech-to-speech model hold the conversation.
The audio quality is better and the barge-in is better, and it would put a
free-form speaker in front of a customer discussing a debt -- which is the
exact thing DLT template registration exists to prevent, and the exact
property the rest of this system is built around not having.

Placing the call is an ordinary contact action. It goes through all eight
gates first: consent for the voice channel, the 08:00-19:00 window in the
recipient's timezone, and ``gate_channel_economics``, which prices a call at
400p against 20p for an SMS and refuses when the expected recovery does not
cover it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any

from recovery.agent.inbound import InboundIntent, InboundReading, keyword_reading
from recovery.domain.case import RecoveryCase
from recovery.domain.events import Actor, EventKind, Ledger
from recovery.domain.failure import DeclineClass, PaymentMethod
from recovery.domain.money import Paise, format_inr, paise
from recovery.policy.actions import ActionKind, Channel, ProposedAction
from recovery.policy.decision import Decision
from recovery.policy.engine import PolicyEngine
from recovery.policy.gates import MessageTemplate, PolicyContext
from recovery.templates import REGISTERED

MAX_UNCLEAR_TURNS = 2
"""How many times the system asks again before releasing the caller. A machine
that keeps repeating a question it cannot get an answer to is a worse
experience than one that ends the call politely."""


class Utterance(StrEnum):
    """Everything the system is capable of saying. Closed set, like the actions."""

    GREET = "greet"
    STATE_BALANCE = "state_balance"
    ASK_DATE = "ask_date"
    CONFIRM_PROMISE = "confirm_promise"
    ACK_STOP = "ack_stop"
    ACK_DISPUTE = "ack_dispute"
    ACK_METHOD = "ack_method"
    OFFER_LINK = "offer_link"
    NOT_UNDERSTOOD = "not_understood"
    CLOSE = "close"


SPOKEN_SLOTS: frozenset[str] = frozenset({"merchant", "plan", "amount", "due_date", "promise_date"})
"""The only values that may appear inside spoken text. Each is derived from the
case, never from what the caller said."""

SCRIPT: dict[Utterance, str] = {
    Utterance.GREET: (
        "Namaste. This is an automated service call from {merchant} about your "
        "{plan} subscription. The recent payment did not go through."
    ),
    Utterance.STATE_BALANCE: (
        "The outstanding amount is {amount}, due since {due_date}. How would you like to handle it?"
    ),
    Utterance.ASK_DATE: "Understood. Which day would you be able to pay?",
    Utterance.CONFIRM_PROMISE: (
        "Noted for {promise_date}. We will not retry before then, and {merchant} "
        "will send you a confirmation by SMS."
    ),
    Utterance.ACK_STOP: (
        "Understood. We will stop contacting you about this. Thank you for your time."
    ),
    Utterance.ACK_DISPUTE: (
        "Thank you. I have marked this as disputed, and collection is paused "
        "while {merchant} reviews it."
    ),
    Utterance.ACK_METHOD: (
        "Noted. {merchant} will send you a link to update the payment method on this subscription."
    ),
    Utterance.OFFER_LINK: "{merchant} will send a payment link to your registered number.",
    Utterance.NOT_UNDERSTOOD: "Sorry, I did not catch that. Could you say it again?",
    Utterance.CLOSE: "Thank you for your time. Goodbye.",
}

_ENDS_CALL: frozenset[Utterance] = frozenset(
    {Utterance.ACK_STOP, Utterance.ACK_DISPUTE, Utterance.CLOSE}
)

VOICE_TEMPLATE = MessageTemplate(
    template_id="RP_VOICE_01",
    channel=Channel.VOICE,
    required_variables=frozenset({"merchant", "amount", "due_date"}),
    purpose="payment_recovery",
)
"""Registered for the duration of a call, via ``PolicyContext.templates``.

*Rejected:* adding it to :data:`recovery.templates.REGISTERED`. That set is what
``proposal_schema`` turns into the enum of templates the planner may name, so
registering a voice utterance globally would silently widen the action space
the model chooses from. A call needs the template; the planner does not."""


def speak(node: Utterance, slots: dict[str, str]) -> str:
    """Render one utterance.

    Raises rather than leaving a slot unfilled: speaking "the outstanding
    amount is {amount}" to a customer is worse than failing loudly, and a
    missing slot means the ledger did not supply a value we claimed to have.
    """
    line = SCRIPT[node]
    try:
        return line.format(**slots)
    except KeyError as exc:
        raise KeyError(f"utterance '{node.value}' needs slot {exc}") from exc


@dataclass
class CallFacts:
    """What the ledger knows, and the only source of anything spoken."""

    merchant: str
    plan: str
    amount_paise: int
    due_date: date
    promise_date: date | None = None

    @property
    def amount(self) -> Paise:
        return paise(self.amount_paise)

    def slots(self) -> dict[str, str]:
        """Bind every spoken value. Nothing here reads a transcript."""
        bound = {
            "merchant": self.merchant,
            "plan": self.plan,
            "amount": format_inr(self.amount),
            "due_date": self.due_date.strftime("%d %B"),
        }
        if self.promise_date is not None:
            bound["promise_date"] = self.promise_date.strftime("%A, %d %B")
        return bound


# --- the turn policy -------------------------------------------------------

_BY_INTENT: dict[InboundIntent, Utterance] = {
    InboundIntent.REQUEST_STOP_RETRIES: Utterance.ACK_STOP,
    InboundIntent.DISPUTE_ALREADY_PAID: Utterance.ACK_DISPUTE,
    InboundIntent.PAYMENT_METHOD_CHANGE: Utterance.ACK_METHOD,
    InboundIntent.GENERAL_QUESTION: Utterance.STATE_BALANCE,
}


def next_utterance(intent: InboundIntent, *, has_date: bool, unclear_count: int) -> Utterance:
    """Which utterance answers this reading. A pure function, deliberately.

    The model decides what was *meant*. What is *said* in response is a lookup,
    so a reading the model got wrong produces the wrong reply from a fixed set
    rather than an unpredictable one.
    """
    if intent is InboundIntent.UNCLEAR:
        return Utterance.CLOSE if unclear_count >= MAX_UNCLEAR_TURNS else Utterance.NOT_UNDERSTOOD
    if intent in (InboundIntent.PROMISE_TO_PAY, InboundIntent.PAYMENT_DATE_CHANGE):
        return Utterance.CONFIRM_PROMISE if has_date else Utterance.ASK_DATE
    return _BY_INTENT.get(intent, Utterance.STATE_BALANCE)


# --- ears ------------------------------------------------------------------

Ears = Callable[[str, date], "tuple[InboundReading, str]"]


def keyword_ears(message: str, today: date) -> tuple[InboundReading, str]:
    """The deterministic baseline R3 measured the model against."""
    return keyword_reading(message, today=today), "keywords"


def model_ears(extractor: Any, *, name: str) -> Ears:
    """Wrap an :class:`~recovery.agent.inbound.InboundExtractor`.

    Reports which path answered, because "the model read that" and "keywords
    read that" are different claims, and R3 is entirely about the difference.
    """

    def read(message: str, today: date) -> tuple[InboundReading, str]:
        reading, used_model = extractor.read(message, today=today)
        return reading, name if used_model else "keywords (model unavailable)"

    return read


# --- a call ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpokenLine:
    node: Utterance
    text: str
    ends_call: bool


@dataclass(frozen=True, slots=True)
class OpenedCall:
    placed: bool
    decision: Decision
    greeting: SpokenLine | None


@dataclass(frozen=True, slots=True)
class VoiceTurn:
    """One exchange: what was heard, what was understood, what was said."""

    heard: str
    heard_by: str
    reading: InboundReading
    model_output: dict[str, Any]
    """Exactly what the reading contract carries. Shown on the console so the
    absence of an amount field is checkable rather than asserted."""

    facts: dict[str, Any]
    say: SpokenLine
    ends_call: bool


@dataclass
class VoiceSession:
    """One call, gated before it is placed and audited turn by turn."""

    case_id: str
    facts: CallFacts
    ledger: Ledger
    now: datetime
    ears: Ears = keyword_ears
    engine: PolicyEngine = field(default_factory=PolicyEngine)

    context: PolicyContext = field(init=False)
    placed: bool = field(default=False, init=False)
    ended: bool = field(default=False, init=False)
    turns: list[VoiceTurn] = field(default_factory=list, init=False)
    _unclear: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.context = PolicyContext(
            case=self._case(),
            now=self.now,
            consented_channels=frozenset({Channel.VOICE, Channel.SMS}),
            consented_purposes=frozenset({VOICE_TEMPLATE.purpose}),
            templates={**REGISTERED, VOICE_TEMPLATE.template_id: VOICE_TEMPLATE},
        )

    def _case(self) -> RecoveryCase:
        return RecoveryCase(
            case_id=self.case_id,
            subscription_id=f"sub_{self.case_id}",
            invoice_id=f"inv_{self.case_id}",
            customer_id=f"cust_{self.case_id}",
            amount=self.facts.amount,
            method=PaymentMethod.EMANDATE,
            decline_reason="insufficient_funds",
            decline_class=DeclineClass.SOFT,
            detected_at=self.now,
        )

    # --- placing the call ---------------------------------------------------

    def open(self) -> OpenedCall:
        """Gate the call, then greet if it is permitted.

        A voice call is an ordinary contact action, so it is evaluated exactly
        like an SMS -- including the contact window in the recipient's timezone
        and the economics of a channel that costs twenty times as much.
        """
        action = ProposedAction(
            kind=ActionKind.SEND_REMINDER,
            channel=Channel.VOICE,
            template_id=VOICE_TEMPLATE.template_id,
            variables={
                name: self.facts.slots()[name] for name in VOICE_TEMPLATE.required_variables
            },
            proposed_by=Actor.OPERATOR,
            rationale="place a recovery call",
        )
        decision = self.engine.evaluate_and_record(action, self.context, self.ledger)
        if not decision.permitted:
            return OpenedCall(placed=False, decision=decision, greeting=None)

        self.placed = True
        greeting = self._line(Utterance.GREET)
        self._record_turn(heard=None, said=greeting, source="system")
        return OpenedCall(placed=True, decision=decision, greeting=greeting)

    # --- one exchange -------------------------------------------------------

    def turn(self, transcript: str) -> VoiceTurn:
        """Hear one thing, understand it, answer from the script."""
        if not self.placed:
            raise RuntimeError("the call was not placed; there is nothing to answer")

        reading, source = self.ears(transcript, self.now.date())
        if reading.intent is InboundIntent.UNCLEAR:
            self._unclear += 1

        facts = reading.policy_facts(now=self.now)
        self._apply(facts, reading)

        node = next_utterance(
            reading.intent,
            has_date=reading.promised_date is not None,
            unclear_count=self._unclear,
        )
        said = self._line(node)
        self.ended = said.ends_call

        turn = VoiceTurn(
            heard=transcript,
            heard_by=source,
            reading=reading,
            model_output=_as_contract(reading),
            facts=facts,
            say=said,
            ends_call=said.ends_call,
        )
        self.turns.append(turn)
        self._record_turn(heard=transcript, said=said, source=source, reading=reading)
        return turn

    def _apply(self, facts: dict[str, Any], reading: InboundReading) -> None:
        """Land the reading's facts in the live context.

        Suppression takes effect *within the call*: once a caller has asked us
        to stop, the next contact action is refused by ``gate_suppression``,
        not by a batch job that runs later.
        """
        if reading.promised_date is not None:
            self.facts.promise_date = reading.promised_date
        self.context = replace_context(self.context, facts)

    def _line(self, node: Utterance) -> SpokenLine:
        return SpokenLine(
            node=node, text=speak(node, self.facts.slots()), ends_call=node in _ENDS_CALL
        )

    def _record_turn(
        self,
        *,
        heard: str | None,
        said: SpokenLine,
        source: str,
        reading: InboundReading | None = None,
    ) -> None:
        payload: dict[str, Any] = {"node": said.node.value, "spoke": said.text, "heard_by": source}
        if heard is not None:
            payload["heard"] = heard
        if reading is not None:
            payload["reading"] = _as_contract(reading)
        self.ledger.record(
            case_id=self.case_id,
            kind=EventKind.PROVIDER_CALLBACK,
            actor=Actor.AGENT if reading is not None else Actor.SYSTEM,
            summary=(
                f"voice: heard '{_clip(heard)}' -> {reading.intent.value}, said {said.node.value}"
                if reading is not None
                else f"voice: call opened, said {said.node.value}"
            ),
            payload=payload,
        )


def replace_context(ctx: PolicyContext, facts: dict[str, Any]) -> PolicyContext:
    """A context with the reading's facts applied. Everything else unchanged."""
    return PolicyContext(
        case=ctx.case,
        now=ctx.now,
        consented_channels=ctx.consented_channels,
        consented_purposes=ctx.consented_purposes,
        dispute_open=bool(facts.get("dispute_open", ctx.dispute_open)),
        do_not_contact=bool(facts.get("do_not_contact", ctx.do_not_contact)),
        subscription_active=ctx.subscription_active,
        promise_to_pay_until=facts.get("promise_to_pay_until", ctx.promise_to_pay_until),
        mandate_active=ctx.mandate_active,
        predebit_notice_sent_at=ctx.predebit_notice_sent_at,
        predebit_opted_out=ctx.predebit_opted_out,
        mandate_category=ctx.mandate_category,
        attempt_carries_afa=ctx.attempt_carries_afa,
        attempts_last_30d=ctx.attempts_last_30d,
        card_network=ctx.card_network,
        downtime_active=ctx.downtime_active,
        downtime_expected_end=ctx.downtime_expected_end,
        last_contact_at=ctx.last_contact_at,
        templates=ctx.templates,
    )


def _as_contract(reading: InboundReading) -> dict[str, Any]:
    """The reading exactly as the extraction schema defines it.

    Rendered on the console so that "there is no amount field" is something a
    viewer checks rather than something they are told.
    """
    return {
        "intent": reading.intent.value,
        "promised_date": reading.promised_date.isoformat() if reading.promised_date else None,
        "requests_no_retry": reading.requests_no_retry,
        "confidence": reading.confidence,
        "verbatim": reading.verbatim,
    }


def _clip(text: str | None, limit: int = 48) -> str:
    if text is None:
        return ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


DEMO_DAYS_OVERDUE = 7


def demo_facts(*, today: date | None = None) -> CallFacts:
    """The case the console calls about.

    Dated relative to today so the call is always about something a week
    overdue. A fixed date would drift into "due since 20 August" long after
    that stopped being a week ago, and a due date of *today* -- the first
    version of this -- had the agent announcing an amount overdue since this
    morning, which is not a thing anyone would call about.
    """
    now = today or datetime.now(UTC).date()
    return CallFacts(
        merchant="Zenmark",
        plan="Pro Annual",
        amount_paise=499900,
        due_date=now - timedelta(days=DEMO_DAYS_OVERDUE),
    )
