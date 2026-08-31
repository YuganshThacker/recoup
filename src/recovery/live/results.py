"""The three measured results, read from the raw output that produced them.

R1, R2 and R3 in one frame is the intellectual core of the pitch: the model
loses at timing, wins at understanding, and the architecture follows that rather
than the other way round.

**Every figure here is parsed from a committed file in ``reports/``.** Not from
constants, not from the documents. A page with ``+23.5`` typed into it would be
a slide, and would go on saying +23.5 long after it stopped being true -- which
is exactly what happened to R1 once already, and is why
``reports/run_r1_recheck.txt`` exists. Reading the raw output means the screen
and the evidence cannot drift apart, and a judge can leave the video, open the
file, and find the number.

**A file that cannot be read reports itself unavailable, never a number.** On a
screen "we could not read it" and "the result is zero" look identical and mean
opposite things, so availability is carried separately from value and the reason
is displayed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPORTS_DIR = Path(__file__).resolve().parents[3] / "reports"

R1_FILE = "run_r1_recheck.txt"
R2_FILE = "run_r2_ablation.txt"
R3_FILE = "run_inbound_bench.txt"

# Narrow on purpose. A loose pattern that matched some other line would be worse
# than failing to parse, because it would render a confident wrong number.
_R1_LIFT = re.compile(r"^\s+lift\s+([-+]\d+\.\d+)\s+95% CI \[([-+][\d.]+), ([-+][\d.]+)\]", re.M)
_R2_OVERALL = re.compile(
    r"^\s+overall\s+([-+]\d+\.\d+)\s+95% CI \[([-+][\d.]+), ([-+][\d.]+)\]\s+n=(\S+)", re.M
)
_R3_DELTA = re.compile(r"^\s+policy facts\s+([-+]\d+)%", re.M)
_R3_P = re.compile(r"^\s+two-sided p\s+([\d.]+)", re.M)
_R3_OVERALL = re.compile(r"^\s+OVERALL\s+\S+\s+\S+\s+\S+\s+(\d+%)", re.M)

MINUS = "−"
"""A real minus sign. R2 is a negative result and a hyphen reads as a dash at
display size, which is the last figure on this screen to be ambiguous about."""


@dataclass(frozen=True, slots=True)
class Result:
    """One measured result, and where it was read from."""

    key: str
    question: str
    headline: str
    detail: str
    verdict: str
    """``system``, ``model_loses`` or ``model_wins`` -- what the page colours by."""

    source: str
    available: bool
    value: float | None = None
    n: str | None = None
    baseline: str | None = None
    model: str | None = None
    reason: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "question": self.question,
            "headline": self.headline,
            "detail": self.detail,
            "verdict": self.verdict,
            "source": self.source,
            "available": self.available,
            "value": self.value,
            "n": self.n,
            "baseline": self.baseline,
            "model": self.model,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class Results:
    r1: Result
    r2: Result
    r3: Result

    def payload(self) -> dict[str, Any]:
        return {
            "r1": self.r1.payload(),
            "r2": self.r2.payload(),
            "r3": self.r3.payload(),
            "mapping": list(MAPPING),
        }


MAPPING: tuple[tuple[str, str], ...] = (
    ("RULES", "TIMING"),
    ("AI", "UNDERSTANDING"),
    ("POLICY", "AUTHORITY"),
    ("AUDIT", "PROOF"),
)
"""What each part of the system is for, in the order the closing frame reads
them. The evidence above is what assigns these roles."""

_QUESTIONS = {
    "R1": "Does the system beat the platform default?",
    "R2": "Does the model beat rules at retry timing?",
    "R3": "Does the model beat keywords at reading customers?",
}

_VERDICTS = {"R1": "system", "R2": "model_loses", "R3": "model_wins"}


def _unavailable(key: str, source: str, reason: str) -> Result:
    return Result(
        key=key,
        question=_QUESTIONS[key],
        headline="unavailable",
        detail=reason,
        verdict=_VERDICTS[key],
        source=source,
        available=False,
        reason=reason,
    )


def _text(directory: Path, name: str) -> str | None:
    path = directory / name
    return path.read_text(encoding="utf-8") if path.is_file() else None


def _points(value: float) -> str:
    """A proportion as display points, with a real minus sign."""
    points = round(value * 100, 1)
    sign = "+" if points >= 0 else MINUS
    return f"{sign}{abs(points):g} pts"


def _read_r1(directory: Path) -> Result:
    body = _text(directory, R1_FILE)
    if body is None:
        return _unavailable("R1", R1_FILE, f"{R1_FILE} not found in {directory}")
    match = _R1_LIFT.search(body)
    if match is None:
        return _unavailable("R1", R1_FILE, f"no lift line in {R1_FILE}")

    value = float(match.group(1))
    return Result(
        key="R1",
        question=_QUESTIONS["R1"],
        headline=_points(value),
        detail=f"95% CI [{match.group(2)}, {match.group(3)}]",
        verdict=_VERDICTS["R1"],
        source=R1_FILE,
        available=True,
        value=value,
    )


def _read_r2(directory: Path) -> Result:
    body = _text(directory, R2_FILE)
    if body is None:
        return _unavailable("R2", R2_FILE, f"{R2_FILE} not found in {directory}")
    match = _R2_OVERALL.search(body)
    if match is None:
        return _unavailable("R2", R2_FILE, f"no overall ablation line in {R2_FILE}")

    value = float(match.group(1))
    return Result(
        key="R2",
        question=_QUESTIONS["R2"],
        headline=_points(value),
        detail=f"95% CI [{match.group(2)}, {match.group(3)}]",
        verdict=_VERDICTS["R2"],
        source=R2_FILE,
        available=True,
        value=value,
        n=match.group(4),
    )


def _read_r3(directory: Path) -> Result:
    body = _text(directory, R3_FILE)
    if body is None:
        return _unavailable("R3", R3_FILE, f"{R3_FILE} not found in {directory}")
    delta, p = _R3_DELTA.search(body), _R3_P.search(body)
    if delta is None or p is None:
        return _unavailable("R3", R3_FILE, f"no policy-facts delta in {R3_FILE}")

    # Two OVERALL rows: the keyword baseline first, then the model. The last
    # column of each is the policy-facts rate, which is what R3 is measured on.
    rates = _R3_OVERALL.findall(body)
    value = float(delta.group(1))
    return Result(
        key="R3",
        question=_QUESTIONS["R3"],
        headline=f"+{value:g} pts",
        detail=f"McNemar p = {p.group(1)}",
        verdict=_VERDICTS["R3"],
        source=R3_FILE,
        available=True,
        value=value,
        baseline=rates[0] if len(rates) > 1 else None,
        model=rates[1] if len(rates) > 1 else None,
    )


def read_results(directory: Path = REPORTS_DIR) -> Results:
    """Parse all three, each falling back independently."""
    return Results(r1=_read_r1(directory), r2=_read_r2(directory), r3=_read_r3(directory))
