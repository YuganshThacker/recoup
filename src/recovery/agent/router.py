"""Arm routing.

Two independent decisions, and keeping them separate is what makes the two
experiments readable:

**R1 -- which arm.** Assigned at detection, stratified, 50/50. Treatment gets
the recovery system; control gets the platform default.

**R2 -- who handles the tail.** Cases reach the tail *because they are hard* --
ambiguous diagnosis, high value, an inbound free-text reply. Comparing
agent-handled tail cases against the easy rules-handled population would measure
the routing, not the model. So once a case is marked tail-eligible, it is
randomised again: half to the agent loop, half to the deterministic fallback.
Both halves stay inside R1's treatment arm, which makes R1's headline a blended
system number and R2 the model number.

Assignment is derived from the case id and a recorded seed, so a run reproduces
exactly and a case always lands in the same arm across reruns.
"""

from __future__ import annotations

import hashlib

from recovery.domain.case import ExperimentArm, RecoveryCase, TailArm
from recovery.planner.base import Planner


def _stable_bit(case_id: str, salt: str) -> bool:
    """A reproducible coin flip for one case.

    Hashing rather than drawing from a shared PRNG means assignment does not
    depend on the order cases happen to be processed in, so adding a case to a
    batch cannot silently reshuffle every case after it.
    """
    digest = hashlib.sha256(f"{salt}:{case_id}".encode()).digest()
    return digest[0] & 1 == 1


class DeterministicArms:
    """R1 only: one planner for treatment, another for control."""

    def __init__(self, treatment: Planner, control: Planner) -> None:
        self._treatment = treatment
        self._control = control

    def planner_for(self, case: RecoveryCase) -> Planner:
        if case.arm is ExperimentArm.CONTROL:
            return self._control
        return self._treatment


class AgentTailArms:
    """R1 plus R2: the agent handles half the tail, rules handle the rest.

    Non-tail treatment cases always take the rules path. Only tail-eligible
    cases are randomised, and only within the treatment arm -- the holdout never
    sees the agent, because R1 measures the system against the platform default.
    """

    def __init__(
        self,
        *,
        rules: Planner,
        control: Planner,
        agent: Planner,
        seed: int,
    ) -> None:
        self._rules = rules
        self._control = control
        self._agent = agent
        self._salt = f"tail-arm-{seed}"

    def planner_for(self, case: RecoveryCase) -> Planner:
        if case.arm is ExperimentArm.CONTROL:
            case.tail_arm = None
            return self._control

        if not case.is_tail:
            case.tail_arm = None
            return self._rules

        if _stable_bit(case.case_id, self._salt):
            case.tail_arm = TailArm.AGENT_LOOP
            return self._agent
        case.tail_arm = TailArm.DETERMINISTIC_FALLBACK
        return self._rules
