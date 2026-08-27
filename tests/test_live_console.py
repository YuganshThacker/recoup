"""Guards on the control room page.

The page is an asset, so these are not rendering tests. They pin the three
things that break silently: the no-network guarantee, the gate matrix drifting
out of step with the policy engine, and a new event kind vanishing from the
lanes because nobody mapped it.
"""

from __future__ import annotations

import re

from recovery.domain.events import EventKind
from recovery.live.console import render_console
from recovery.policy.decision import GateName

PAGE = render_console()


def test_the_page_carries_a_title() -> None:
    assert "<title>Recoup Control Room</title>" in PAGE


def test_the_page_makes_no_external_request() -> None:
    # The audit report holds this line and so does the console: a demo that goes
    # dark because a venue blocked a font host is an avoidable way to lose a
    # room. Same-origin /api paths and inline data: URIs are the only fetches.
    #
    # xmlns declarations are stripped first. They look like URLs and are not:
    # an XML namespace is an identifier a browser never dereferences, and the
    # inline SVG favicon needs one.
    fetchable = re.sub(r"xmlns='[^']*'", "", PAGE)
    external = re.findall(r"""["'(](?:https?:)?//[^"')\s]+""", fetchable)

    assert external == [], f"the console would reach the network for {external}"


def test_the_gate_matrix_matches_the_policy_engine() -> None:
    # A gate added to the engine and not to this list would evaluate on every
    # decision and appear nowhere -- the exact failure the matrix exists to
    # rule out.
    listed = re.search(r"const GATES=\[(.*?)\];", PAGE, re.S)
    assert listed is not None
    names = set(re.findall(r'"([a-z_]+)"', listed.group(1)))

    assert names == {gate.value for gate in GateName}


def test_every_event_kind_lands_in_a_lane() -> None:
    # An unmapped kind is dropped by render(), so a new event type would be
    # written to the ledger and never shown.
    lanes = re.search(r"const LANES=\[(.*?)\];", PAGE, re.S)
    assert lanes is not None
    mapped = set(re.findall(r'"([a-z_]+)"', lanes.group(1)))

    unmapped = {kind.value for kind in EventKind} - mapped
    assert unmapped == set(), f"these event kinds would never appear: {sorted(unmapped)}"


def test_write_requests_from_the_page_carry_the_console_header() -> None:
    # Without it the run button 403s against its own server.
    assert '"X-Recoup-Console"' in PAGE


def test_the_money_readouts_declare_where_their_number_came_from() -> None:
    # The project's rule is no financial figure without a traceable source.
    # Here that rule is a hover, and these are the elements that must carry it.
    for element_id in ("ro-rec", "ro-spent"):
        block = re.search(rf'id="{element_id}"[^>]*', PAGE)
        assert block is not None, f"{element_id} missing"
        assert "data-src=" in block.group(0), f"{element_id} states no source"
