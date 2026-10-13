"""Send the digest by SMTP (Proton SMTP submission: STARTTLS on 587, token as password).

The token is read only from JOB_SCOUT_SMTP_TOKEN, here, at send time; it's never a setting.
"""

from __future__ import annotations

import smtplib
import ssl
from collections.abc import Callable, Mapping
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid

from job_scout.config.settings import Email

TOKEN_ENV = "JOB_SCOUT_SMTP_TOKEN"

SmtpFactory = Callable[[str, int, float], smtplib.SMTP]


class EmailError(RuntimeError):
    pass


def _smtp(host: str, port: int, timeout: float) -> smtplib.SMTP:
    return smtplib.SMTP(host, port, timeout=timeout)


def build_message(
    settings: Email, subject: str, text: str, html: str, now: datetime
) -> EmailMessage:
    assert settings.from_address and settings.to_address
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.from_address
    message["To"] = settings.to_address
    message["Date"] = format_datetime(now)
    message["Message-ID"] = make_msgid(domain=settings.from_address.rpartition("@")[2])
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    return message


def send(
    settings: Email,
    message: EmailMessage,
    environ: Mapping[str, str],
    smtp_factory: SmtpFactory | None = None,
) -> None:
    """Raise EmailError with a reason the CLI can print; never include the token in it."""
    token = environ.get(TOKEN_ENV)
    if not token:
        raise EmailError(f"{TOKEN_ENV} is not set")
    assert settings.from_address
    try:
        connect = smtp_factory or _smtp
        with connect(settings.smtp_host, settings.smtp_port, settings.timeout_seconds) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(settings.from_address, token)
            smtp.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        raise EmailError(f"{type(exc).__name__}: {exc}") from exc
