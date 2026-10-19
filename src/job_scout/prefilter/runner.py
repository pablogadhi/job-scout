"""Apply the pre-filter to stored jobs and report on it."""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import Select, case, delete, exists, func, or_, select
from sqlalchemy.orm import Session, aliased, sessionmaker

from job_scout.config import AppConfig
from job_scout.db.groups import load_groups
from job_scout.db.models import Job, PrefilterResult
from job_scout.db.types import utcnow
from job_scout.prefilter.rules import Prefilter

BATCH = 500


def build_prefilter(config: AppConfig) -> Prefilter:
    return Prefilter(config.filters, {s.id: s.prefilter for s in config.sources.sources})


def pending_query(fingerprint: str, rerun: bool = False) -> Select[int]:
    """Canonical jobs whose group needs a (new) verdict.

    A verdict goes stale when the rules change (fingerprint) or when a copy was linked to
    the group after it was evaluated (e.g. the same job posted later for another country).
    """
    query = select(Job.id).where(Job.duplicate_of.is_(None)).order_by(Job.id)
    if rerun:
        return query
    copy = aliased(Job)
    newer_copy = exists().where(
        copy.duplicate_of == Job.id, copy.first_seen_at > PrefilterResult.evaluated_at
    )
    return query.outerjoin(PrefilterResult, PrefilterResult.job_id == Job.id).where(
        or_(
            PrefilterResult.job_id.is_(None),
            PrefilterResult.fingerprint != fingerprint,
            newer_copy,
        )
    )


def forget_verdicts(session: Session, job_ids: Sequence[int]) -> None:
    """Drop these groups' verdicts (their text changed): the next `filter` judges them as new."""
    if job_ids:
        session.execute(delete(PrefilterResult).where(PrefilterResult.job_id.in_(job_ids)))


@dataclass(frozen=True)
class FilterRunResult:
    evaluated: int
    passed: int


def run_prefilter(
    config: AppConfig, session_factory: sessionmaker[Session], *, rerun: bool = False
) -> FilterRunResult:
    prefilter = build_prefilter(config)
    with session_factory() as session:
        ids = list(session.scalars(pending_query(prefilter.fingerprint, rerun)))
    evaluated = passed = 0
    for start in range(0, len(ids), BATCH):
        with session_factory.begin() as session:
            now = utcnow()
            for group in load_groups(session, ids[start : start + BATCH]):
                verdict = prefilter.evaluate_group(group.jobs)
                session.merge(
                    PrefilterResult(
                        job_id=group.canonical.id,
                        passed=verdict.passed,
                        reason=verdict.reason,
                        evaluated_at=now,
                        fingerprint=prefilter.fingerprint,
                        group_size=len(group.jobs),
                    )
                )
                evaluated += 1
                passed += verdict.passed
    return FilterRunResult(evaluated=evaluated, passed=passed)


@dataclass(frozen=True)
class SourceReport:
    source_id: str
    jobs: int  # every stored posting from this source
    duplicates: int  # postings linked to an earlier copy (judged with their group)
    evaluated: int  # canonical jobs with a verdict
    passed: int

    @property
    def dropped(self) -> int:
        return self.evaluated - self.passed

    @property
    def pass_rate(self) -> float | None:
        return self.passed / self.evaluated if self.evaluated else None


def source_report(session: Session) -> list[SourceReport]:
    rows = session.execute(
        select(
            Job.source_id,
            func.count(Job.id),
            func.count(Job.duplicate_of),
            func.count(PrefilterResult.job_id),
            func.coalesce(func.sum(case((PrefilterResult.passed, 1), else_=0)), 0),
        )
        .outerjoin(PrefilterResult, PrefilterResult.job_id == Job.id)
        .group_by(Job.source_id)
        .order_by(Job.source_id)
    )
    return [SourceReport(*row) for row in rows]


def drop_reasons(session: Session, limit: int = 10) -> list[tuple[str, int]]:
    """Most common drop rules, with group sizes and matched text stripped for grouping."""
    reasons = session.scalars(
        select(PrefilterResult.reason).where(PrefilterResult.passed.is_(False))
    )
    counts: dict[str, int] = {}
    for reason in reasons:
        key = _rule_of(reason or "")
        counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])[:limit]


def _rule_of(reason: str) -> str:
    """'remote: '\\bhybrid\\b' matched 'Hybrid' in location [3 copies]' -> the rule part."""
    reason = reason.split(" [", 1)[0]
    if " matched " in reason:
        return reason.split(" matched ", 1)[0]
    if " (" in reason and reason.startswith("title: excluded"):
        return reason.split(" (", 1)[0]
    if reason.startswith("salary:"):
        return "salary: below floor"
    return reason


def sample(
    session: Session, *, passed: bool, n: int, seed: int | None = None
) -> list[tuple[Job, PrefilterResult]]:
    """Random canonical jobs with the given verdict, for spot checks with `explain`."""
    rows = [
        (job, result)
        for job, result in session.execute(
            select(Job, PrefilterResult)
            .join(PrefilterResult, PrefilterResult.job_id == Job.id)
            .where(PrefilterResult.passed.is_(passed))
        )
    ]
    return random.Random(seed).sample(rows, min(n, len(rows)))
