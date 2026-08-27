"""Cold open tests.

The hero is the thirty seconds before the console appears. It has to work with
no video file present -- the asset is produced separately and may not exist
when the demo runs -- so the typographic fallback is the real design, and the
video is an upgrade.

The media route is the only place this server reads a file off disk, which
makes it the only place path traversal could exist. It takes no parameter at
all, by construction.
"""

from __future__ import annotations

import re
from pathlib import Path

from recovery.live.hero import HERO_FILES, find_hero_media, render_hero

PAGE_WITHOUT_VIDEO = render_hero(media=None)


def test_the_cold_open_stands_up_without_a_video() -> None:
    assert "<title>" in PAGE_WITHOUT_VIDEO
    assert "<video" not in PAGE_WITHOUT_VIDEO


def test_it_says_where_to_put_the_video_when_there_is_none() -> None:
    # Otherwise the absence looks like a bug rather than an empty slot.
    assert "assets/hero" in PAGE_WITHOUT_VIDEO


def test_it_embeds_the_video_when_one_exists() -> None:
    page = render_hero(media="hero.mp4")

    assert "<video" in page
    assert "/hero/media" in page


def test_both_forms_lead_into_the_console() -> None:
    for page in (PAGE_WITHOUT_VIDEO, render_hero(media="hero.mp4")):
        assert 'href="/"' in page


def test_the_cold_open_fetches_nothing() -> None:
    # xmlns declarations are stripped first: an XML namespace looks like a URL
    # and is never dereferenced, and the inline SVG favicon needs one.
    fetchable = re.sub(r"xmlns='[^']*'", "", PAGE_WITHOUT_VIDEO)
    external = re.findall(r"""["'(](?:https?:)?//[^"')\s]+""", fetchable)

    assert external == []


def test_the_media_lookup_only_considers_known_filenames() -> None:
    # The route takes no parameter, so there is no user input to traverse with.
    # This pins that the candidate list stays a fixed allowlist.
    assert all("/" not in name and "\\" not in name and ".." not in name for name in HERO_FILES)


def test_no_media_directory_is_not_an_error(tmp_path: Path) -> None:
    assert find_hero_media(tmp_path / "nothing") is None


def test_it_finds_a_file_that_is_there(tmp_path: Path) -> None:
    (tmp_path / "hero.mp4").write_bytes(b"\x00\x00\x00\x18ftyp")

    assert find_hero_media(tmp_path) == "hero.mp4"


def test_it_prefers_the_first_listed_format(tmp_path: Path) -> None:
    for name in HERO_FILES:
        (tmp_path / name).write_bytes(b"x")

    assert find_hero_media(tmp_path) == HERO_FILES[0]


def test_a_directory_named_like_the_video_is_not_treated_as_one(tmp_path: Path) -> None:
    (tmp_path / HERO_FILES[0]).mkdir()

    assert find_hero_media(tmp_path) is None
