"""Credential loading tests.

Small surface, but it handles secrets, so the properties that matter are
asserted rather than assumed: an exported value is never overridden, a missing
file is not an error, and no value is ever rendered into a string that could
reach a log.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from recovery.env import (
    CredentialError,
    _parse,
    describe_credentials,
    load_dotenv,
    validate_credential,
)


def test_parses_plain_and_quoted_values() -> None:
    parsed = _parse("A=1\nB=\"two\"\nC='three'\n")
    assert parsed == {"A": "1", "B": "two", "C": "three"}


def test_ignores_blanks_and_comments() -> None:
    parsed = _parse("\n# a comment\n\nA=1\n   \nnot-a-pair\n")
    assert parsed == {"A": "1"}


def test_values_containing_equals_survive() -> None:
    # Keys are commonly base64-ish and can contain '='.
    assert _parse("K=abc=def==")["K"] == "abc=def=="


def test_exported_value_is_not_overridden(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # An exported variable is a more explicit statement of intent than a file
    # left over from last week. Overriding it is how the wrong account gets
    # billed.
    env_file = tmp_path / ".env"
    env_file.write_text("RECOVERY_TEST_KEY=from-file\n")
    monkeypatch.setenv("RECOVERY_TEST_KEY", "from-export")

    loaded = load_dotenv(env_file)

    assert loaded == []
    assert os.environ["RECOVERY_TEST_KEY"] == "from-export"


def test_loads_values_that_are_not_already_set(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    env_file = tmp_path / ".env"
    env_file.write_text("RECOVERY_TEST_UNSET=from-file\n")
    monkeypatch.delenv("RECOVERY_TEST_UNSET", raising=False)

    assert load_dotenv(env_file) == ["RECOVERY_TEST_UNSET"]
    assert os.environ["RECOVERY_TEST_UNSET"] == "from-file"


def test_missing_file_is_not_an_error(tmp_path: Path) -> None:
    # Exporting is an equally valid way to configure a run.
    assert load_dotenv(tmp_path / "nope.env") == []


def test_load_reports_key_names_only(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # The return value is printed on live runs, so it must never carry a value.
    env_file = tmp_path / ".env"
    env_file.write_text("RECOVERY_TEST_SECRET=sk-super-secret-value\n")
    monkeypatch.delenv("RECOVERY_TEST_SECRET", raising=False)

    loaded = load_dotenv(env_file)

    assert loaded == ["RECOVERY_TEST_SECRET"]
    assert "sk-super-secret-value" not in "".join(loaded)


def test_describe_credentials_never_reveals_a_value(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("OPENAI_API_KEY", "sk-super-secret-value")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    described = describe_credentials()

    assert described == "OPENAI_API_KEY"
    assert "sk-" not in described


def test_describe_credentials_says_none_when_absent(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert describe_credentials() == "none"


# --- credential shape ------------------------------------------------------


def test_missing_credential_is_named() -> None:
    with pytest.raises(CredentialError, match="OPENAI_API_KEY is not set"):
        validate_credential("OPENAI_API_KEY", None)


def test_placeholder_is_rejected() -> None:
    with pytest.raises(CredentialError, match="still the placeholder"):
        validate_credential("OPENAI_API_KEY", "paste-your-key-here")


def test_whitespace_is_rejected() -> None:
    with pytest.raises(CredentialError, match="whitespace"):
        validate_credential("OPENAI_API_KEY", "sk-abc123 ")


def test_smart_dash_is_caught_and_explained() -> None:
    # The real failure this exists for: a key pasted through something that
    # reformats text. Without this the SDK raises UnicodeEncodeError from deep
    # inside the HTTP stack, which says nothing about the cause.
    with pytest.raises(CredentialError) as info:
        validate_credential("OPENAI_API_KEY", "sk-proj\u2013abc")
    message = str(info.value)
    assert "non-ASCII" in message
    assert "typographic dashes" in message
    # Never the value itself.
    assert "sk-proj" not in message


def test_valid_credential_passes() -> None:
    validate_credential("OPENAI_API_KEY", "sk-proj-" + "a" * 100)
