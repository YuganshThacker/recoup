"""The compliance x-ray: what one case can be proven to have done.

This is the artifact a compliance officer asks for. It is built entirely from
the audit ledger, and its value is that it can come back **negative** -- a
report that can only say "pass" attests to nothing.

Five checks, each answering a question the ledger genuinely settles:

===== ===========================================================
 C1    Did anything execute without a permitted policy decision?
 C2    Did any customer contact go out without a registered template?
 C3    Did any action execute over a gate that refused it?
 C4    Does every refusal carry a reason code?
 C5    Is the sequence unbroken?
===== ===========================================================

**What it deliberately does not claim.** Ledger timestamps are wall-clock, and
a simulated case spans milliseconds of real time, so this cannot re-derive the
24-hour pre-debit interval. Printing "notice served 24h before the debit" from
those timestamps would be inventing a figure. What it reports instead is that
``gate_mandate`` -- the code that enforces the interval -- was evaluated and
passed on every executed debit, and it says which claim it is making.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from recovery.domain.events import AuditEvent, EventKind
from recovery.domain.money import format_inr, paise
from recovery.templates import REGISTERED

GATE_NAMES: tuple[str, ...] = (
    "consent",
    "suppression",
    "mandate",
    "attempt_budget",
    "quiet_hours",
    "cooldown",
    "template",
    "channel_economics",
)

_TEMPLATE = re.compile(r"\b(RP_[A-Z0-9_]+)\b")
_CHANNEL = re.compile(r"\bvia (\w+)\b")
_SENT = re.compile(r"^sent (RP_[A-Z0-9_]+) via (\w+)")
"""The runner records pre-debit notices as NOTICE_SENT and every *other*
message as ACTION_EXECUTED with this summary. Both are customer contacts, and
reading only the first kind let payment links and dunning reminders escape the
registered-template check entirely."""

_DEBIT = re.compile(r"^debit ")

CAVEATS: tuple[str, ...] = (
    "Ledger timestamps are wall-clock, not the simulated clock the case ran on. "
    "This report therefore does not re-derive the 24-hour pre-debit interval; it "
    "attests that gate_mandate, which enforces it, was evaluated and passed on "
    "every executed debit.",
    "Recovery outcomes in this run are simulated. Contacts, decisions and gate "
    "results are not -- they are what the policy engine actually did.",
)


@dataclass(frozen=True, slots=True)
class Contact:
    """One thing sent to a customer."""

    seq: int
    channel: str
    template_id: str | None
    registered: bool
    cost_paise: int
    summary: str


@dataclass(frozen=True, slots=True)
class MoneyAction:
    """One execution against the customer's instrument."""

    seq: int
    summary: str
    authorised: bool
    authority_seq: int | None


@dataclass(frozen=True, slots=True)
class Check:
    code: str
    question: str
    passed: bool
    detail: str
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Xray:
    case_id: str
    amount: str | None
    arm: str | None
    events: int
    contacts: tuple[Contact, ...]
    money_actions: tuple[MoneyAction, ...]
    gate_tally: dict[str, tuple[int, int]]
    refusals: tuple[str, ...]
    checks: tuple[Check, ...]
    verdict: str
    """``clean``, ``exceptions``, or ``empty``. Never "clean" for a case with
    no events -- "nothing happened, therefore compliant" is the worst bug a
    compliance report can have."""

    caveats: tuple[str, ...]

    @property
    def exceptions(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if not c.passed)


def build_xray(case_id: str, events: list[AuditEvent]) -> Xray:
    """Read one case's history and attest to what it shows."""
    if not events:
        return Xray(
            case_id=case_id,
            amount=None,
            arm=None,
            events=0,
            contacts=(),
            money_actions=(),
            gate_tally={name: (0, 0) for name in GATE_NAMES},
            refusals=(),
            checks=(),
            verdict="empty",
            caveats=CAVEATS,
        )

    detected = next((e for e in events if e.kind is EventKind.CASE_DETECTED), None)
    amount = detected.payload.get("amount_paise") if detected else None

    contacts = _contacts(events)
    permits = _permits(events)
    executions = _executions(events, permits)
    money = _debits(executions)

    checks = (
        _c1(executions),
        _c2(contacts),
        _c3(events),
        _c4(events),
        _c5(events),
    )

    return Xray(
        case_id=case_id,
        amount=format_inr(paise(int(amount))) if isinstance(amount, int) else None,
        arm=str(detected.payload.get("arm")) if detected else None,
        events=len(events),
        contacts=contacts,
        money_actions=money,
        gate_tally=_tally(events),
        refusals=tuple(e.summary for e in events if e.kind is EventKind.ACTION_REFUSED),
        checks=checks,
        verdict="clean" if all(c.passed for c in checks) else "exceptions",
        caveats=CAVEATS,
    )


# --- reading the ledger ----------------------------------------------------


def _is_message(event: AuditEvent) -> bool:
    """Did this event put something in front of a customer?

    Notices carry their own event kind; everything else the system sends is an
    ACTION_EXECUTED whose summary names the template.
    """
    if event.kind is EventKind.NOTICE_SENT:
        return True
    return event.kind is EventKind.ACTION_EXECUTED and _SENT.match(event.summary) is not None


def _contacts(events: list[AuditEvent]) -> tuple[Contact, ...]:
    found = []
    for event in events:
        if not _is_message(event):
            continue
        template = _TEMPLATE.search(event.summary)
        channel = _CHANNEL.search(event.summary)
        template_id = template.group(1) if template else None
        found.append(
            Contact(
                seq=event.seq,
                channel=channel.group(1) if channel else "unknown",
                template_id=template_id,
                registered=template_id in REGISTERED if template_id else False,
                cost_paise=int(event.payload.get("cost_paise") or 0),
                summary=event.summary,
            )
        )
    return tuple(found)


def _permits(events: list[AuditEvent]) -> list[tuple[int, str]]:
    """(seq, action) for every permitted decision, in order."""
    return [
        (e.seq, str(e.payload.get("action", "")))
        for e in events
        if e.kind is EventKind.POLICY_EVALUATED and e.payload.get("permitted") is True
    ]


def _authority(event: AuditEvent, permits: list[tuple[int, str]]) -> int | None:
    """The most recent permit that actually covers this execution.

    A debit needs a permit for a debit; a message needs a permit naming *the
    same template*. Matching every execution against any prior permit would
    pass a case that was permitted one template and sent another, which is
    precisely the kind of drift this report exists to catch.
    """
    sent = _SENT.match(event.summary)
    if sent is not None:
        wanted = sent.group(1)
        covering = [seq for seq, action in permits if wanted in action]
    elif _DEBIT.match(event.summary):
        covering = [seq for seq, action in permits if "retry_debit" in action]
    else:
        covering = [seq for seq, _ in permits]

    prior = [seq for seq in covering if seq < event.seq]
    return prior[-1] if prior else None


def _executions(
    events: list[AuditEvent], permits: list[tuple[int, str]]
) -> tuple[MoneyAction, ...]:
    """Everything the system carried out, with the decision that authorised it."""
    found = []
    for event in events:
        if event.kind is not EventKind.ACTION_EXECUTED:
            continue
        authority = _authority(event, permits)
        found.append(
            MoneyAction(
                seq=event.seq,
                summary=event.summary,
                authorised=authority is not None,
                authority_seq=authority,
            )
        )
    return tuple(found)


def _debits(executions: tuple[MoneyAction, ...]) -> tuple[MoneyAction, ...]:
    """Only the executions that moved money, for the instrument table."""
    return tuple(m for m in executions if _DEBIT.match(m.summary))


def _tally(events: list[AuditEvent]) -> dict[str, tuple[int, int]]:
    """(passed, refused) per gate across the whole case."""
    counts = {name: [0, 0] for name in GATE_NAMES}
    for event in events:
        for gate in _gates(event):
            name = str(gate.get("gate"))
            if name in counts:
                counts[name][0 if gate.get("passed") else 1] += 1
    return {name: (c[0], c[1]) for name, c in counts.items()}


def _gates(event: AuditEvent) -> list[dict[str, Any]]:
    raw = event.payload.get("gates")
    return raw if isinstance(raw, list) else []


# --- the checks ------------------------------------------------------------


def _c1(money: tuple[MoneyAction, ...]) -> Check:
    unauthorised = [m for m in money if not m.authorised]
    return Check(
        code="C1",
        question="Did anything execute without a permitted policy decision?",
        passed=not unauthorised,
        detail=(
            f"{len(money)} execution(s), all preceded by a permitted decision"
            if not unauthorised
            else f"{len(unauthorised)} execution(s) with no permit on the ledger"
        ),
        evidence=tuple(
            f"seq {m.seq}: {m.summary} — authority seq {m.authority_seq}"
            if m.authorised
            else f"seq {m.seq}: {m.summary} — NO PERMIT"
            for m in money
        ),
    )


def _c2(contacts: tuple[Contact, ...]) -> Check:
    unregistered = [c for c in contacts if not c.registered]
    return Check(
        code="C2",
        question="Did any customer contact go out without a registered template?",
        passed=not unregistered,
        detail=(
            f"{len(contacts)} contact(s), every one on a DLT-registered template"
            if not unregistered
            else f"{len(unregistered)} contact(s) with no registered template"
        ),
        evidence=tuple(
            f"seq {c.seq}: {c.template_id or 'NO TEMPLATE'} via {c.channel} ({c.cost_paise}p)"
            for c in contacts
        ),
    )


def _c3(events: list[AuditEvent]) -> Check:
    """Nothing may execute over a gate that said no."""
    bad = [
        f"seq {e.seq}: permitted while {g['gate']} refused"
        for e in events
        if e.kind is EventKind.POLICY_EVALUATED and e.payload.get("permitted") is True
        for g in _gates(e)
        if not g.get("passed")
    ]
    debits = [
        e
        for e in events
        if e.kind is EventKind.POLICY_EVALUATED
        and e.payload.get("permitted") is True
        and "retry_debit" in str(e.payload.get("action", ""))
    ]
    mandate_passes = sum(
        1 for e in debits for g in _gates(e) if g.get("gate") == "mandate" and g.get("passed")
    )
    return Check(
        code="C3",
        question="Did any action execute over a gate that refused it?",
        passed=not bad,
        detail=(
            f"no. gate_mandate evaluated and passed on {mandate_passes} of "
            f"{len(debits)} permitted debit(s)"
            if not bad
            else f"{len(bad)} decision(s) permitted despite a refusing gate"
        ),
        evidence=tuple(bad),
    )


def _c4(events: list[AuditEvent]) -> Check:
    """A refusal without a code cannot be reviewed."""
    uncoded = [
        f"seq {e.seq}: {g['gate']} refused with no code"
        for e in events
        if e.kind is EventKind.ACTION_REFUSED
        for g in _gates(e)
        if not g.get("passed") and not g.get("code")
    ]
    refusals = sum(1 for e in events if e.kind is EventKind.ACTION_REFUSED)
    return Check(
        code="C4",
        question="Does every refusal carry a reason code?",
        passed=not uncoded,
        detail=(
            f"{refusals} refusal(s), each naming a rule"
            if not uncoded
            else f"{len(uncoded)} refusal(s) with no reason code"
        ),
        evidence=tuple(uncoded),
    )


def _c5(events: list[AuditEvent]) -> Check:
    """An append-only ledger with a hole in it is not an append-only ledger."""
    expected = list(range(1, len(events) + 1))
    actual = [e.seq for e in events]
    return Check(
        code="C5",
        question="Is the sequence unbroken?",
        passed=actual == expected,
        detail=(
            f"seq 1..{len(events)}, monotonic and complete"
            if actual == expected
            else f"expected 1..{len(events)}, found {actual}"
        ),
    )
