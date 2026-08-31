"""Results screen tests.

The screen exists to put R1, R2 and R3 in one frame. What makes it worth
building rather than a slide is that every figure is parsed from the raw output
committed in `reports/`, so a judge can leave the video, open the file, and find
the number.

Most of these tests defend that: the parse must reproduce the published figures,
and a file it cannot read must say so rather than render a zero. On a screen "we
could not read it" and "the result is zero" look identical and mean opposite
things.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

from recovery.live.results import REPORTS_DIR, Results, read_results
from recovery.live.results_page import render_results

REAL = read_results(REPORTS_DIR)


# --- the published figures --------------------------------------------------


def test_r1_matches_the_committed_recheck() -> None:
    r1 = REAL.r1

    assert r1.available, r1.reason
    assert r1.value == 0.2353
    assert "+0.1741" in r1.detail and "+0.2964" in r1.detail
    assert r1.source == "run_r1_recheck.txt"


def test_r2_matches_the_committed_ablation() -> None:
    r2 = REAL.r2

    assert r2.available, r2.reason
    assert r2.value == -0.2124
    assert "-0.3326" in r2.detail and "-0.0922" in r2.detail
    assert r2.n == "120/119"
    assert r2.source == "run_r2_ablation.txt"


def test_r3_matches_the_committed_bench() -> None:
    r3 = REAL.r3

    assert r3.available, r3.reason
    assert r3.value == 12.0
    assert "0.0019" in r3.detail
    assert r3.source == "run_inbound_bench.txt"


def test_r3_carries_the_two_rates_that_make_the_delta_concrete() -> None:
    # +12 means little on its own; 78% against 90% on policy facts means
    # something a viewer can hold.
    assert REAL.r3.baseline == "78%"
    assert REAL.r3.model == "90%"


def test_the_headlines_are_formatted_in_the_module_not_the_page() -> None:
    # So the page has no arithmetic that could drift from the parse.
    assert REAL.r1.headline == "+23.5 pts"
    assert REAL.r2.headline == "−21.2 pts"
    assert REAL.r3.headline == "+12 pts"


def test_the_verdicts_do_not_flatter_the_model() -> None:
    # R2 is the result that goes against us and the screen has to say so.
    assert REAL.r1.verdict == "system"
    assert REAL.r2.verdict == "model_loses"
    assert REAL.r3.verdict == "model_wins"


# --- what happens when a file cannot be read --------------------------------


def test_a_missing_file_reports_unavailable_rather_than_a_number(tmp_path: Path) -> None:
    results = read_results(tmp_path)

    for result in (results.r1, results.r2, results.r3):
        assert result.available is False
        assert result.value is None
        assert result.reason and "not found" in result.reason


def test_a_file_that_does_not_parse_reports_unavailable(tmp_path: Path) -> None:
    (tmp_path / "run_r1_recheck.txt").write_text("this is not a batch report\n")

    result = read_results(tmp_path).r1

    assert result.available is False
    assert result.value is None
    assert result.reason and "no lift line" in result.reason


def test_one_unreadable_file_does_not_take_the_others_down(tmp_path: Path) -> None:
    (tmp_path / "run_r2_ablation.txt").write_text((REPORTS_DIR / "run_r2_ablation.txt").read_text())

    results = read_results(tmp_path)

    assert results.r2.available is True
    assert results.r1.available is False


def test_the_payload_serialises() -> None:
    json.dumps(REAL.payload())


# --- the page ---------------------------------------------------------------


PAGE = render_results(REAL)


def test_no_figure_is_hardcoded_in_the_source() -> None:
    """The whole argument is that these come from committed files.

    The page renders server-side, so the figures *are* in its output -- that is
    correct. What must not happen is a figure typed into the module, which would
    keep saying +23.5 long after it stopped being true. That already happened
    once to R1, and is why run_r1_recheck.txt exists.

    Stylesheets are stripped: hex colours are full of digits and match anything,
    which already produced a false positive on the race page.
    """
    for module in ("results.py", "results_page.py"):
        source = (Path("src/recovery/live") / module).read_text()
        # Docstrings and comments explain the rule and quote the figures while
        # doing so; stylesheets are full of hex digits. Only code is in scope.
        source = re.sub(r'"""(?:.|\n)*?"""', "", source)
        source = re.sub(r"#.*", "", source)

        for figure in ("23.5", "21.2", "0.2353", "0.2124", "0.0019", "78%", "90%"):
            assert not re.search(rf"(?<![\w.]){re.escape(figure)}(?![\w.])", source), (
                f"{figure} is written into {module} instead of parsed from a report"
            )


def test_the_page_renders_whatever_it_is_given() -> None:
    # The proof that it is data-driven rather than a template with the real
    # numbers baked in: hand it different results, get different numbers.
    invented = Results(
        r1=replace(REAL.r1, headline="+99.9 pts", detail="95% CI [+1.0, +2.0]"),
        r2=replace(REAL.r2, headline="-88.8 pts", detail="95% CI [-9.0, -8.0]"),
        r3=replace(REAL.r3, headline="+77 pts", detail="McNemar p = 0.5"),
    )

    page = render_results(invented)

    assert "+99.9 pts" in page and "-88.8 pts" in page and "+77 pts" in page
    assert "+23.5 pts" not in page


def test_an_unavailable_result_says_so_rather_than_showing_a_figure(tmp_path: Path) -> None:
    page = render_results(read_results(tmp_path))

    assert "unavailable" in page
    assert "not found" in page
    assert "pts" not in page


def test_the_page_names_the_file_each_figure_came_from() -> None:
    for source in ("run_r1_recheck.txt", "run_r2_ablation.txt", "run_inbound_bench.txt"):
        assert source in PAGE


def test_the_page_carries_the_mapping_the_script_closes_on() -> None:
    for line in ("RULES", "TIMING", "UNDERSTANDING", "AUTHORITY", "PROOF"):
        assert line in PAGE


def test_the_page_fetches_nothing() -> None:
    fetchable = re.sub(r"xmlns='[^']*'", "", PAGE)

    assert re.findall(r"""["'(](?:https?:)?//[^"')\s]+""", fetchable) == []
