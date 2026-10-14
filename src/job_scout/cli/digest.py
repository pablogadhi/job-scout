"""`digest` and `notify-failure`: the emails."""

from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo

import typer
from rich.markup import escape
from sqlalchemy.orm import Session, sessionmaker

from job_scout.cli.common import DryRunOpt, console, err_console, load, open_db
from job_scout.config import AppConfig
from job_scout.db.types import utcnow
from job_scout.report.build import make_digest
from job_scout.report.failure import FailureReport, log_tail, send_failure_email
from job_scout.report.mailer import EmailError


def digest(ctx: typer.Context, dry_run: DryRunOpt = False) -> None:
    """Write the digest of new matches (HTML) and email it when email is enabled.

    --dry-run writes reports/digest-preview.html and sends or records nothing.
    """
    config = load(ctx)
    send_digest(config, open_db(config), dry_run=dry_run)


def notify_failure(
    ctx: typer.Context,
    unit: Annotated[str, typer.Option("--unit", help="The failed systemd unit.")] = "job-scout",
    result: Annotated[
        str | None, typer.Option("--result", help="systemd's MONITOR_SERVICE_RESULT.")
    ] = None,
    exit_status: Annotated[
        str | None, typer.Option("--exit-status", help="systemd's MONITOR_EXIT_STATUS.")
    ] = None,
) -> None:
    """Email a failed-run report; the run's log comes on stdin (systemd OnFailure= unit)."""
    config = load(ctx)
    journal = "" if sys.stdin.isatty() else sys.stdin.read()
    report = FailureReport(unit, result or None, exit_status or None, log_tail(journal))
    now = datetime.now(ZoneInfo(config.settings.schedule.timezone))
    try:
        send_failure_email(config.settings.digest.email, report, os.environ, now)
    except EmailError as exc:
        err_console.print(f"[red]could not send the failure email:[/red] {escape(str(exc))}")
        err_console.print(report.text(now), markup=False)
        raise typer.Exit(1) from exc
    console.print(f"failure report emailed: {report.subject}")


def send_digest(
    config: AppConfig, session_factory: sessionmaker[Session], *, dry_run: bool = False
) -> None:
    """Write (and unless dry_run, email and record) the digest; exit 1 if the email failed."""
    outcome = make_digest(
        config, session_factory, now=utcnow(), environ=os.environ, dry_run=dry_run
    )
    s = outcome.sections
    console.print(
        f"digest: {len(s.watchlist)} watchlist, {len(s.top)} top, {len(s.unclear)} unclear "
        f"location -> {outcome.path}"
    )
    email = config.settings.digest.email
    if dry_run:
        console.print("[bold]dry run[/bold]: preview only; nothing emailed or recorded")
    elif outcome.emailed:
        console.print(f"emailed to {', '.join(email.recipients)}")
    elif outcome.emailed is False:
        err_console.print(
            f"[red]email failed:[/red] {escape(outcome.email_error or '?')}; not recorded, "
            "so the next digest carries these jobs again"
        )
        raise typer.Exit(1)
    elif outcome.skipped_empty:
        console.print("nothing new: email skipped (digest.send_when_empty is false)")
    elif not email.enabled:
        console.print("email disabled (digest.email.enabled); HTML file only")
