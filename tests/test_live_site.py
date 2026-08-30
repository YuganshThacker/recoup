"""Static evidence site tests.

The site is what someone reviewing the submission asynchronously clicks
through: the audit report and a set of compliance attestations, with no server
and no control surface. It is generated from a real run rather than assembled
by hand, so it can be rebuilt after any change and cannot drift from the code.
"""

from __future__ import annotations

from pathlib import Path

from recovery.live.site import build_site, pick_cases


def test_it_writes_an_index_and_at_least_one_xray(tmp_path: Path) -> None:
    manifest = build_site(tmp_path, cases=12)

    assert (tmp_path / "index.html").is_file()
    assert manifest.xrays, "a site with no attestations is not evidence"
    for entry in manifest.xrays:
        assert (tmp_path / entry.path).is_file()


def test_the_index_links_every_page_it_generated(tmp_path: Path) -> None:
    manifest = build_site(tmp_path, cases=12)
    index = (tmp_path / "index.html").read_text()

    for entry in manifest.xrays:
        assert entry.path in index


def test_it_carries_the_audit_report_when_one_exists(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "audit.html").write_text("<title>audit</title>")

    manifest = build_site(tmp_path / "out", cases=8, audit_report=source / "audit.html")

    assert manifest.audit is not None
    assert (tmp_path / "out" / manifest.audit).is_file()


def test_a_missing_audit_report_is_not_an_error(tmp_path: Path) -> None:
    # The report is produced by a separate run and may not have been made yet.
    manifest = build_site(tmp_path, cases=8, audit_report=tmp_path / "nope.html")

    assert manifest.audit is None
    assert (tmp_path / "index.html").is_file()


def test_it_disables_jekyll_processing(tmp_path: Path) -> None:
    # Without this, GitHub Pages runs the output through Jekyll and drops
    # anything it decides looks like a template.
    build_site(tmp_path, cases=8)

    assert (tmp_path / ".nojekyll").is_file()


def test_the_index_fetches_nothing(tmp_path: Path) -> None:
    import re

    build_site(tmp_path, cases=8)
    page = re.sub(r"xmlns='[^']*'", "", (tmp_path / "index.html").read_text())
    external = re.findall(r"""["'(](?:https?:)?//[^"')\s]+""", page)

    assert all("github.com" in url for url in external), (
        f"the index reaches beyond the repository link: {external}"
    )


def test_case_selection_prefers_cases_worth_reading(tmp_path: Path) -> None:
    # An attestation on a case where nothing happened proves nothing. The
    # selection favours cases with refusals, so the checks have something to
    # check.
    manifest = build_site(tmp_path, cases=24)

    assert any(entry.refusals > 0 for entry in manifest.xrays)


def test_selection_is_bounded(tmp_path: Path) -> None:
    manifest = build_site(tmp_path, cases=24, max_xrays=3)

    assert len(manifest.xrays) <= 3


def test_a_case_the_report_faulted_is_surfaced_first(tmp_path: Path) -> None:
    # The findings are the point. Surfacing them only by luck of the sort order
    # would bury the one thing the site exists to show.
    manifest = build_site(tmp_path, cases=60, max_xrays=4)

    if any(e.verdict == "exceptions" for e in manifest.xrays):
        assert manifest.xrays[0].verdict == "exceptions"


def test_pick_cases_handles_an_empty_run() -> None:
    assert pick_cases({}, limit=5) == []
