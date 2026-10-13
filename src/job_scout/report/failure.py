"""The failed-run email: systemd's OnFailure unit pipes the run's log into `notify-failure`.

There's no external monitor (DECISIONS.md P6-U1): the daily digest is the heartbeat, and this
email reports a run that failed or hung (killed by TimeoutStartSec) within minutes.
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from job_scout.config.settings import Email
from job_scout.report import mailer

MAX_LOG_LINES = 120
MAX_LOG_CHARS = 30_000

# Logs never print secrets, but a third-party error message could echo one: mask anything
# shaped like an Anthropic key or OAuth token, and NAME=value pairs for our secret variables.
_SECRETS = re.compile(
    r"sk-ant-[A-Za-z0-9_\-]{8,}"
    r"|(?:ANTHROPIC_API_KEY|CLAUDE_CODE_OAUTH_TOKEN|JOB_SCOUT_SMTP_TOKEN|FINDWORK_API_KEY)"
    r"=\S+"
)


def redact(text: str) -> str:
    return _SECRETS.sub("[redacted]", text)


def log_tail(text: str) -> str:
    lines = redact(text).splitlines()[-MAX_LOG_LINES:]
    return "\n".join(lines)[-MAX_LOG_CHARS:]


@dataclass(frozen=True)
class FailureReport:
    unit: str
    result: str | None  # systemd's MONITOR_SERVICE_RESULT: exit-code, timeout, signal, ...
    exit_status: str | None
    log: str

    @property
    def subject(self) -> str:
        why = "timed out" if self.result == "timeout" else f"failed ({self.result or 'unknown'})"
        return f"job-scout: daily run {why}"

    def text(self, now: datetime) -> str:
        return (
            f"{self.unit} {self.subject.removeprefix('job-scout: ')} at {now:%Y-%m-%d %H:%M %Z}.\n"
            f"Result: {self.result or '?'}, exit status: {self.exit_status or '?'}.\n"
            "The digest may be missing or incomplete today.\n\n"
            f"Last log lines:\n\n{self.log or '(no log captured)'}\n"
        )

    def html(self, now: datetime) -> str:
        return f"<pre>{html.escape(self.text(now))}</pre>"


def send_failure_email(
    settings: Email,
    report: FailureReport,
    environ: Mapping[str, str],
    now: datetime,
    smtp_factory: mailer.SmtpFactory | None = None,
) -> None:
    """Raise mailer.EmailError when it can't be sent (email off, no token, SMTP error)."""
    if not settings.enabled:
        raise mailer.EmailError("email is disabled (digest.email.enabled)")
    message = mailer.build_message(
        settings, report.subject, report.text(now), report.html(now), now
    )
    mailer.send(settings, message, mailer.read_token(environ), smtp_factory)
