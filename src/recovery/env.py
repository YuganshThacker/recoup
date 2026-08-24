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


def describe_credentials() -> str:
    """Report which provider credentials are present, without revealing them."""
    present = [name for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY") if os.environ.get(name)]
    return ", ".join(present) if present else "none"
