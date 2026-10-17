"""Save and read votes. One row per canonical job; a new vote replaces the old one."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from job_scout.db.models import Feedback, Score, ScoreStatus, Vote
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
        row.overall_score = result.overall_score
        row.disqualified = result.disqualified
        row.summary = result.summary
    session.flush()
    return row


def get_vote(session: Session, job_id: int) -> Feedback | None:
    return session.scalars(select(Feedback).where(Feedback.job_id == job_id)).one_or_none()
