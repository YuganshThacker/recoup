"""Minimal .env loader.

Dependency-free, because the point of this project's dependency story is that
the core runs with nothing installed. It does one thing: read ``KEY=value``
lines from a ``.env`` at the project root into the process environment.

Two rules, both deliberate:

* **A value already in the environment always wins.** An exported variable is a
  more explicit statement of intent than a file left over from last week, and
  silently overriding it is how people end up billing the wrong account.
* **Nothing here ever logs a value.** Only key names are ever reported, and only
  on request.

``.env`` is gitignored. Secrets belong in it, never in source, never in a
commit, and never in a shell history line.
"""

from __future__ import annotations

import os
from pathlib import Path


def project_root() -> Path:
    """The repository root, two levels above this package."""
    return Path(__file__).resolve().parents[2]


def _parse(text: str) -> dict[str, str]:
    """Parse KEY=value lines, ignoring blanks and comments."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Strip one layer of matching quotes, so both KEY=abc and KEY="abc" work.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_dotenv(path: Path | None = None) -> list[str]:
    """Load ``.env`` into ``os.environ``. Returns the key names it set.

    Missing file is not an error -- exported variables are an equally valid way
    to configure a run, and a live batch should not require a file to exist.
    """
    target = path or project_root() / ".env"
    if not target.is_file():
        return []

    loaded: list[str] = []
    for key, value in _parse(target.read_text(encoding="utf-8")).items():
        if key in os.environ:
            continue  # an exported value is more explicit; leave it alone
        os.environ[key] = value
        loaded.append(key)
    return loaded


# Dash-like characters that rich-text fields substitute for a plain hyphen.
# A key is ASCII, so any of these means the value was pasted through something
# that reformats text -- and if it changed a dash it may have changed more.
_SMART_DASHES = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"

_PLACEHOLDERS = {"", "paste-your-key-here", "your-key-here", "changeme", "sk-..."}


class CredentialError(Exception):
    """A credential is missing or malformed in a way worth naming precisely."""


def validate_credential(name: str, value: str | None) -> None:
    """Check a credential's shape, without ever revealing it.

    Catching this here turns three unhelpful failures into one clear message: a
    non-ASCII character raises a UnicodeEncodeError from deep inside the HTTP
    stack, a placeholder produces a bare 401, and whitespace from a sloppy paste
    produces the same 401 with nothing to distinguish it.
    """
    if not value:
        raise CredentialError(f"{name} is not set")
    if value in _PLACEHOLDERS:
        raise CredentialError(f"{name} is still the placeholder value")
    if value != value.strip():
        raise CredentialError(f"{name} has leading or trailing whitespace")
    if not value.isascii():
        positions = [i for i, c in enumerate(value) if not c.isascii()]
        smart = [i for i in positions if value[i] in _SMART_DASHES]
        detail = f"non-ASCII character(s) at index {positions}"
        if smart:
            detail += (
                " -- these are typographic dashes, so the key was pasted through "
                "something that reformats text (a notes app, a doc, a chat). "
                "Re-copy it and paste straight into .env"
            )
        raise CredentialError(f"{name} contains {detail}")


def describe_credentials() -> str:
    """Report which provider credentials are present, without revealing them."""
    present = [name for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY") if os.environ.get(name)]
    return ", ".join(present) if present else "none"
