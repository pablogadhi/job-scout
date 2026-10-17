"""Which job groups need a score.

One score row per canonical job. A job is scored once: it's queued again only by `rescore`
or when a copy is linked to its group, never because profile.md changed. Pending rows don't
belong to a backend, so a backlog can start on one backend and finish on the other.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast

from sqlalchemy import (
    CursorResult,
    Result,
    Select,
    exists,
    func,
    insert,
    literal,
    or_,
    select,
    update,
)
from sqlalchemy.orm import Session, aliased

from job_scout.db.models import (
    CallStatus,
    Job,
    PrefilterResult,
    Score,
    ScoreStatus,
    ScoringCall,
)

MAX_ATTEMPTS = 2  # a job is retried once after a failed call, then marked score_failed


def _eligible() -> Select[int]:
    """Canonical jobs whose group passed the pre-filter."""
    return (
        select(Job.id)
        .join(PrefilterResult, PrefilterResult.job_id == Job.id)
        .where(Job.duplicate_of.is_(None), PrefilterResult.passed.is_(True))
    )


def enqueue(session: Session, profile_hash: str) -> tuple[int, int]:
    """Add pending scores for eligible groups that have none; re-queue groups that changed.

    A scored group is re-queued when a copy was linked to it after it was scored (e.g. the
    same job posted later for another country), because its combined locations changed.
    `profile_hash` is recorded on the new rows. Returns (new, requeued).
    """
    has_score = exists().where(Score.job_id == Job.id)
    new_ids = select(Job.id, literal(profile_hash)).where(Job.id.in_(_eligible()), ~has_score)
    new = _rowcount(session.execute(insert(Score).from_select(["job_id", "profile_hash"], new_ids)))

    copy = aliased(Job)
    newer_copy = exists().where(
        copy.duplicate_of == Score.job_id, copy.first_seen_at > Score.scored_at
    )
    requeued = _rowcount(
        session.execute(
            update(Score)
            .where(
                Score.status == ScoreStatus.SCORED,
                Score.scored_at.is_not(None),
                newer_copy,
            )
            .values(status=ScoreStatus.PENDING, attempts=0)
            .execution_options(synchronize_session=False)
        )
    )
    return new, requeued


def _rowcount(result: Result[Any]) -> int:
    """Rows an INSERT/UPDATE touched (DML results are CursorResults at runtime)."""
    return cast("CursorResult[Any]", result).rowcount or 0


def _in_flight() -> Select[int]:
    """Calls whose results haven't been ingested (a submitted batch)."""
    return select(ScoringCall.id).where(
        ScoringCall.status.in_([CallStatus.SUBMITTED, CallStatus.RUNNING])
    )


def pending_query(order: str = "newest_first") -> Select[int]:
    """Canonical job ids to score, in scoring order; excludes jobs inside an unfinished batch."""
    newest = order == "newest_first"
    posted = Job.posted_at.desc() if newest else Job.posted_at.asc()
    seen = Job.first_seen_at.desc() if newest else Job.first_seen_at.asc()
    return (
        select(Job.id)
        .join(Score, Score.job_id == Job.id)
        .where(
            Score.status == ScoreStatus.PENDING,
            Job.id.in_(_eligible()),
            or_(Score.call_id.is_(None), Score.call_id.not_in(_in_flight())),
        )
        .order_by(Job.posted_at.is_(None), posted, seen, Job.id)
    )


def pending_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(pending_query().subquery())) or 0


def rescore_ids(
    session: Session,
    *,
    top: int | None = None,
    since: datetime | None = None,
    legacy: bool = False,
) -> list[int]:
    """Jobs to score again: the top N by score, eligible jobs first seen since a date,
    and/or (`legacy`) every job scored before points (it has no breakdown)."""
    ids: list[int] = []
    if top:
        ids += session.scalars(
            select(Score.job_id)
            .where(Score.status == ScoreStatus.SCORED)
            .order_by(Score.overall_score.desc())
            .limit(top)
        )
    if since:
        ids += session.scalars(_eligible().where(Job.first_seen_at >= since))
    if legacy:
        # In Python: the JSON column stores None as JSON null, which SQL's IS NULL misses.
        scored = session.scalars(
            select(Score).where(Score.status == ScoreStatus.SCORED).order_by(Score.job_id)
        )
        ids += [s.job_id for s in scored if s.result is not None and not s.breakdown]
    return list(dict.fromkeys(ids))


def requeue_for_rescore(
    session: Session,
    profile_hash: str,
    *,
    top: int | None = None,
    since: datetime | None = None,
    legacy: bool = False,
) -> int:
    """Queue `rescore_ids` to be scored again under the current profile. Returns jobs queued."""
    ids = rescore_ids(session, top=top, since=since, legacy=legacy)
    return _set_pending(session, profile_hash, ids)


def undo_requeue(session: Session, before: dict[int, tuple[ScoreStatus, int] | None]) -> None:
    """Put rows back as they were before a requeue (`snapshot`): a dry run plans with them
    pending, then nothing stays queued."""
    for job_id, state in before.items():
        score = session.scalars(select(Score).where(Score.job_id == job_id)).one_or_none()
        if score is None:
            continue
        if state is None:
            session.delete(score)
        else:
            score.status, score.attempts = state
    session.flush()


def snapshot(session: Session, job_ids: Sequence[int]) -> dict[int, tuple[ScoreStatus, int] | None]:
    rows = {
        s.job_id: (s.status, s.attempts)
        for s in session.scalars(select(Score).where(Score.job_id.in_(job_ids)))
    }
    return {job_id: rows.get(job_id) for job_id in job_ids}


def _set_pending(session: Session, profile_hash: str, job_ids: Sequence[int]) -> int:
    for job_id in job_ids:
        score = session.scalars(select(Score).where(Score.job_id == job_id)).one_or_none()
        if score is None:
            session.add(Score(job_id=job_id, profile_hash=profile_hash))
        else:
            score.status, score.attempts = ScoreStatus.PENDING, 0
    session.flush()
    return len(job_ids)
