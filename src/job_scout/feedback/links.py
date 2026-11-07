"""Signed feedback links. A link names one job; its signature proves the digest made it, so
nobody can vote on other jobs by editing the number in the URL. The dashboard's link carries
a token signed over a different message, so it can't stand in for a job's signature, nor the
other way round.

The secret is read only from JOB_SCOUT_FEEDBACK_SECRET, never from a file in git.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlencode

from pydantic import SecretStr

from job_scout.config.secrets import secret
from job_scout.config.settings import Feedback as FeedbackSettings

SECRET_ENV = "JOB_SCOUT_FEEDBACK_SECRET"
MIN_SECRET_CHARS = 32  # `openssl rand -hex 32` gives 64


class SecretError(RuntimeError):
    pass


def read_secret(environ: Mapping[str, str]) -> SecretStr:
    """Raise SecretError if the secret is unset or too short to sign with."""
    value = secret(environ, SECRET_ENV)
    if value is None:
        raise SecretError(f"{SECRET_ENV} is not set (generate one with `openssl rand -hex 32`)")
    if len(value.get_secret_value()) < MIN_SECRET_CHARS:
        raise SecretError(f"{SECRET_ENV} is too short: use at least {MIN_SECRET_CHARS} characters")
    return value


def _sign(secret: SecretStr, message: str) -> str:
    key = secret.get_secret_value().encode()
    digest = hmac.new(key, message.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest[:16]).decode().rstrip("=")


def sign(secret: SecretStr, job_id: int) -> str:
    return _sign(secret, f"job-scout feedback {job_id}")


def _matches(expected: str, given: str) -> bool:
    # Bytes: compare_digest raises TypeError on a str with non-ASCII characters.
    return hmac.compare_digest(expected.encode(), given.encode())


def verify(secret: SecretStr, job_id: int, signature: str) -> bool:
    return _matches(sign(secret, job_id), signature)


def sign_dashboard(secret: SecretStr) -> str:
    return _sign(secret, "job-scout dashboard")


def verify_dashboard(secret: SecretStr, token: str) -> bool:
    return _matches(sign_dashboard(secret), token)


@dataclass(frozen=True)
class FeedbackLinks:
    base_url: str
    secret: SecretStr

    def url(self, job_id: int, vote: str) -> str:
        query = urlencode({"v": vote, "s": sign(self.secret, job_id)})
        return f"{self.base_url}/f/{job_id}?{query}"

    def dashboard_url(self) -> str:
        return f"{self.base_url}/d?" + urlencode({"k": sign_dashboard(self.secret)})


def feedback_links(settings: FeedbackSettings, environ: Mapping[str, str]) -> FeedbackLinks | None:
    """Links for the digest, or None while base_url or the secret is missing (no links)."""
    if not settings.base_url:
        return None
    try:
        secret = read_secret(environ)
    except SecretError:
        return None
    return FeedbackLinks(settings.base_url, secret)
