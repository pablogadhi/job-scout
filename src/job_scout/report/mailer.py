"""Send the digest by SMTP (Proton SMTP submission: STARTTLS on 587, token as password).

The token is read only from JOB_SCOUT_SMTP_TOKEN, here, at send time; it's never a setting.
"""

from __future__ import annotations

import smtplib
import ssl
from collections.abc import Callable, Mapping
from datetime import datetime
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from types import TracebackType
from typing import Protocol

from pydantic import SecretStr

from job_scout.config.secrets import secret
from job_scout.config.settings import Email

TOKEN_ENV = "JOB_SCOUT_SMTP_TOKEN"
TO_ENV = "JOB_SCOUT_DIGEST__EMAIL__TO_ADDRESS"  # the setting's env var (digest.email.to_address)


class SmtpConnection(Protocol):
    """What `send` uses of an SMTP connection: smtplib.SMTP, or a fake in tests."""

    def __enter__(self) -> SmtpConnection: ...
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
        /,
    ) -> object: ...
    def starttls(self, *, context: ssl.SSLContext) -> object: ...
    def login(self, user: str, password: str, /) -> object: ...
    def send_message(self, msg: EmailMessage, /) -> object: ...


SmtpFactory = Callable[[str, int, float], SmtpConnection]


class EmailError(RuntimeError):
    pass


def _smtp(host: str, port: int, timeout: float) -> SmtpConnection:
    return smtplib.SMTP(host, port, timeout=timeout)


def build_message(
    settings: Email, subject: str, text: str, html: str, now: datetime
) -> EmailMessage:
    assert settings.from_address
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = Address(
        display_name=settings.from_name or "", addr_spec=settings.from_address
    )
    message["To"] = ", ".join(settings.recipients)
    message["Date"] = format_datetime(now)
    message["Message-ID"] = make_msgid(domain=settings.from_address.rpartition("@")[2])
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    return message


def read_token(environ: Mapping[str, str]) -> SecretStr | None:
    """The SMTP token, or None when unset."""
    return secret(environ, TOKEN_ENV)


def send(
    settings: Email,
    message: EmailMessage,
    token: SecretStr | None,
    smtp_factory: SmtpFactory | None = None,
) -> None:
    """Raise EmailError with a reason the CLI can print; never include the token in it."""
    if token is None:
        raise EmailError(f"{TOKEN_ENV} is not set")
    if not settings.recipients:
        raise EmailError(f"no recipient: set {TO_ENV}")
    assert settings.from_address
    try:
        connect = smtp_factory or _smtp
        with connect(settings.smtp_host, settings.smtp_port, settings.timeout_seconds) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(settings.from_address, token.get_secret_value())
            smtp.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        raise EmailError(f"{type(exc).__name__}: {exc}") from exc
