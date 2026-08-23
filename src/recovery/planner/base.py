"""The planner contract.

One protocol, implemented by the two deterministic planners and by the agent
planner. Defining it here rather than in any one implementation keeps the
runner, the router and the agent from depending on each other -- each depends on
the shape, not on a particular planner.
"""

from __future__ import annotations

from typing import Protocol

from recovery.domain.case import RecoveryCase
from recovery.planner.rules import Plan, PlannerFacts


class Planner(Protocol):
    """Decides the next step for a case, or that there is no next step."""

    def next_step(self, case: RecoveryCase, facts: PlannerFacts) -> Plan: ...
