"""Save and read votes, applications and "no longer available" marks.

Votes: one row per canonical job; a new vote replaces the old one. Applications: one row per
company you applied to (a job without a company stands for itself). No longer available: a
date on every job of the group (`Job.unavailable_at`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from job_scout.db.groups import Group
from job_scout.db.models import Application, Feedback, Job, Score, ScoreStatus, Vote
from job_scout.scoring.schema import CoreResult

MAX_REASON_CHARS = 1000


def current_score(session: Session, job_id: int) -> Score | None:
    """The job's score, if it has been scored."""
    return session.scalars(
        select(Score).where(Score.job_id == job_id, Score.status == ScoreStatus.SCORED)
    ).one_or_none()


def record_vote(
    session: Session,
    job_id: int,
    vote: Vote,
    reason: str | None,
    now: datetime,
) -> Feedback:
    """Store (or replace) the vote, with a copy of the score it reacts to."""
    reason = (reason or "").strip()[:MAX_REASON_CHARS] or None
    row = session.scalars(select(Feedback).where(Feedback.job_id == job_id)).one_or_none()
    if row is None:
        row = Feedback(job_id=job_id, vote=vote, created_at=now)
        session.add(row)
    row.vote, row.reason, row.updated_at = vote, reason, now
    score = current_score(session, job_id)
    if score is None or score.result is None:
        row.profile_hash = row.overall_score = row.disqualified = row.summary = None
    else:
        result = CoreResult.model_validate(score.result)
        row.profile_hash = score.profile_hash
        row.overall_score = score.overall_score
        row.disqualified = score.disqualified
        row.summary = result.summary
    session.flush()
    return row


def get_vote(session: Session, job_id: int) -> Feedback | None:
    return session.scalars(select(Feedback).where(Feedback.job_id == job_id)).one_or_none()


def votes(session: Session) -> dict[int, Feedback]:
    """Every vote, by canonical job id."""
    return {row.job_id: row for row in session.scalars(select(Feedback))}


@dataclass(frozen=True)
class ApplicationKey:
    """What an application is stored under: the company's normalized name, or the job
    itself when it has no company."""

    company_normalized: str | None
    job_id: int | None

    @classmethod
    def of(cls, job: Job) -> ApplicationKey:
        if job.company_normalized:
            return cls(job.company_normalized, None)
        return cls(None, job.id)

    @property
    def text(self) -> str:
        """A stable string form, for the page's markup."""
        return f"c:{self.company_normalized}" if self.company_normalized else f"j:{self.job_id}"


def applications(session: Session) -> dict[ApplicationKey, Application]:
    return {
        ApplicationKey(row.company_normalized, row.job_id): row
        for row in session.scalars(select(Application))
    }


def set_applied(session: Session, group: Group, applied: bool, now: datetime) -> ApplicationKey:
    """Mark (or unmark) the group's company as applied to; a job without a company is marked
    on its own. Marking again keeps the first date."""
    job = group.canonical
    key = ApplicationKey.of(job)
    where = (
        Application.company_normalized == key.company_normalized
        if key.company_normalized
        else Application.job_id == key.job_id
    )
    if not applied:
        session.execute(delete(Application).where(where))
    elif session.scalars(select(Application).where(where)).one_or_none() is None:
        session.add(
            Application(
                company_normalized=key.company_normalized,
                company=job.company,
                job_id=key.job_id,
                job_title=job.title,
                applied_at=now,
            )
        )
    session.flush()
    return key


def set_unavailable(
    session: Session, group: Group, unavailable: bool, now: datetime
) -> datetime | None:
    """Mark (or unmark) every job of the group as no longer available; marking again keeps
    the first date. Returns the group's date, None when unmarked."""
    when = (group.canonical.unavailable_at or now) if unavailable else None
    for job in group.jobs:
        job.unavailable_at = when
    session.flush()
    return when
