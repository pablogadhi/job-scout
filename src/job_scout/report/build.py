"""Build, write, send and record the daily digest and the backlog report."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from job_scout.config import AppConfig
from job_scout.db.models import Digest, DigestKind
from job_scout.feedback.links import feedback_links
from job_scout.report import mailer
from job_scout.report.data import (
    ReportItem,
    Sections,
    build_sections,
    reported_job_ids,
    scored_items,
    source_health,
    watchlist_names,
)
from job_scout.report.render import Rendered, render, write_report
from job_scout.scoring.status import scoring_status

log = logging.getLogger(__name__)


@dataclass
class DigestOutcome:
    path: Path
    sections: Sections
    emailed: bool | None = None  # None: email disabled, or a dry run
    email_error: str | None = None
    recorded: bool = False  # its jobs won't be reported again
    skipped_empty: bool = False


def _render(
    config: AppConfig,
    session: Session,
    sections: Sections,
    *,
    heading: str,
    now: datetime,
    since: datetime | None,
    include_below: bool,
    environ: Mapping[str, str],
) -> Rendered:
    scoring = config.settings.scoring
    status = scoring_status(
        session,
        config.settings.schedule.timezone,
        scoring.cli.token_created,
        now,
        backend=scoring.backend,
    )
    return render(
        config,
        heading=heading,
        sections=sections,
        status=status,
        health=source_health(session, config, environ),
        now=now,
        since=since,
        include_below=include_below,
        feedback=feedback_links(config.settings.feedback, environ),
    )


def make_digest(
    config: AppConfig,
    session_factory: sessionmaker[Session],
    *,
    now: datetime,
    environ: Mapping[str, str],
    dry_run: bool = False,
    smtp_factory: mailer.SmtpFactory | None = None,
) -> DigestOutcome:
    """New matches since earlier digests/backlog reports. A dry run writes only a preview file.

    The digest is recorded (its jobs never repeated) once delivered: written to disk, and
    emailed when email is enabled. A failed email leaves it unrecorded, so the next digest
    carries the same jobs.
    """
    settings = config.settings.digest
    with session_factory() as session:
        previous = session.scalars(
            select(Digest).where(Digest.kind == DigestKind.DIGEST).order_by(Digest.id.desc())
        ).first()
        items = scored_items(
            session,
            watchlist_names(config),
            exclude=reported_job_ids(session),
        )
        cutoff = now - timedelta(days=settings.rescored_max_age_days)
        since = previous.sent_at if previous else None
        items = [i for i in items if _new_enough(i, since, cutoff)]
        sections = build_sections(items, settings, include_below=False)
        rendered = _render(
            config,
            session,
            sections,
            heading="job-scout digest",
            now=now,
            since=previous.sent_at if previous else None,
            include_below=False,
            environ=environ,
        )
    reports = config.settings.paths.reports
    if dry_run:
        path = reports / "digest-preview.html"
        reports.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered.html, encoding="utf-8")
        return DigestOutcome(path, sections)

    path = write_report(reports, "digest", now, rendered.html)
    outcome = DigestOutcome(path, sections)
    email = settings.email
    if email.enabled:
        if not sections and not settings.send_when_empty:
            outcome.skipped_empty = True
        else:
            message = mailer.build_message(
                email, rendered.subject, rendered.text, rendered.html, now
            )
            try:
                mailer.send(email, message, mailer.read_token(environ), smtp_factory)
                outcome.emailed = True
            except mailer.EmailError as exc:
                outcome.emailed, outcome.email_error = False, str(exc)
                log.error("digest email failed: %s", exc)
    if outcome.emailed is not False:
        with session_factory.begin() as session:
            session.add(
                Digest(
                    kind=DigestKind.DIGEST,
                    sent_at=now,
                    job_ids=sections.job_ids,
                    report_path=str(path),
                    emailed=bool(outcome.emailed),
                )
            )
        outcome.recorded = True
    return outcome


def _new_enough(item: ReportItem, since: datetime | None, cutoff: datetime) -> bool:
    """Scored for the first time since the previous digest, or first seen after `cutoff`.
    The other jobs reach this digest only because their score changed later."""
    first = item.score.first_scored_at
    if since is None or first is None or first > since:
        return True
    return max(j.first_seen_at for j in item.group.jobs) >= cutoff


@dataclass
class BacklogOutcome:
    path: Path
    sections: Sections


def make_backlog_report(
    config: AppConfig,
    session_factory: sessionmaker[Session],
    *,
    now: datetime,
    environ: Mapping[str, str],
) -> BacklogOutcome:
    """Every scored group, ranked, including below-threshold ones.

    Its reported jobs (watchlist, top, unclear) are recorded, so later digests show only
    what's new.
    """
    with session_factory() as session:
        items = scored_items(session, watchlist_names(config))
        sections = build_sections(items, config.settings.digest, include_below=True)
        rendered = _render(
            config,
            session,
            sections,
            heading="job-scout backlog report",
            now=now,
            since=None,
            include_below=True,
            environ=environ,
        )
    path = write_report(config.settings.paths.reports, "backlog", now, rendered.html)
    with session_factory.begin() as session:
        session.add(
            Digest(
                kind=DigestKind.BACKLOG,
                sent_at=now,
                job_ids=sections.job_ids,
                report_path=str(path),
            )
        )
    return BacklogOutcome(path, sections)
