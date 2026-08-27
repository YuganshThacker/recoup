"""The live surface: control room, red team, voice, compliance x-ray.

This package is a *viewer* over the system, never a participant in it. Nothing
here decides an action, moves money, or changes what the batch runner does. The
one integration point is :class:`~recovery.live.broadcast.BroadcastLedger`,
which satisfies the existing ``LedgerStore`` protocol so the runner takes it
without changing a line.

That separation is not tidiness. The numbers in ``docs/RESULTS.md`` come from
runs whose behaviour must not depend on whether a browser tab was open.
"""
