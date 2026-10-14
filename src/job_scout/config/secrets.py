"""Secrets come only from the environment, never from files in git.

Every secret the code reads goes through `secret()`, which wraps it in SecretStr so no repr,
log line or traceback can show it; call `get_secret_value()` only where it's handed over
(an HTTP header, the SMTP login, the SDK client).
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import SecretStr


def secret(environ: Mapping[str, str], name: str) -> SecretStr | None:
    """The variable's value without surrounding whitespace, or None when unset or blank."""
    value = environ.get(name, "").strip()
    return SecretStr(value) if value else None
