"""Write fetched postings: drop already-seen ones, link cross-posted duplicates, and fill in
a job first stored without its description once a fetch brings it."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from job_scout.db.models import Job
from job_scout.fetch.models import Posting
from job_scout.fetch.normalize import dedupe_key, normalize_company, normalize_title

_CHUNK = 500  # stay well under SQLite's bound-parameter limit


@dataclass(frozen=True)
class StoreResult:
    new: int = 0  # first copy of a job
    duplicate: int = 0  # new posting linked to an earlier copy from any source
    seen: int = 0  # this source's posting was already stored; last_seen_at bumped
    filled: int = 0  # of those, stored without a description and now given one


def _key(job: Job, fields: Sequence[str]) -> str:
    values = [getattr(job, f) for f in fields]
    if not all(values):
        # Missing parts (e.g. no company) would merge unrelated jobs; keep it unique instead.
        return dedupe_key(["unique", job.source_id, job.source_job_id])
    return dedupe_key(values)


def _chunks[T](items: Sequence[T]) -> list[Sequence[T]]:
    return [items[i : i + _CHUNK] for i in range(0, len(items), _CHUNK)]


# What identifies a job and groups its copies never changes once stored.
_KEPT = frozenset({"source_job_id", "title", "dedupe_title", "company"})


def _fill(session: Session, existing: dict[str, int], by_id: dict[str, Posting]) -> int:
    """A job stored from a list without its details (a Workday title the filters dropped at
    the time) gets them from a later fetch that has them."""
    described = [sid for sid in existing if by_id[sid].description_text]
    filled = 0
    for chunk in _chunks(described):
        bare = session.scalars(
            select(Job).where(
                Job.id.in_([existing[sid] for sid in chunk]), Job.description_text.is_(None)
            )
        )
        for job in bare:
            for name, value in by_id[job.source_job_id].model_dump().items():
                if name not in _KEPT and value is not None:
                    setattr(job, name, value)
            filled += 1
    return filled


def store_postings(
    session: Session,
    source_id: str,
    postings: Sequence[Posting],
    dedupe_fields: Sequence[str],
    now: datetime,
) -> StoreResult:
    first_by_id: dict[str, Posting] = {}
    for posting in postings:  # a job can appear in several of a source's feeds
        first_by_id.setdefault(posting.source_job_id, posting)
    unique = list(first_by_id.values())
    ids = [p.source_job_id for p in unique]

    existing: dict[str, int] = {}
    for chunk in _chunks(ids):
        rows = session.execute(
            select(Job.source_job_id, Job.id).where(
                Job.source_id == source_id, Job.source_job_id.in_(chunk)
            )
        )
        existing.update({sid: jid for sid, jid in rows})
    for chunk in _chunks(list(existing.values())):
        session.execute(update(Job).where(Job.id.in_(chunk)).values(last_seen_at=now))
    filled = _fill(session, existing, first_by_id)

    canonical: dict[str, int] = {}
    new = duplicate = 0
    for posting in unique:
        if posting.source_job_id in existing:
            continue
        job = Job(
            source_id=source_id,
            **posting.model_dump(),
            company_normalized=normalize_company(posting.company),
            title_normalized=normalize_title(posting.dedupe_title or posting.title),
            first_seen_at=now,
            last_seen_at=now,
        )
        job.dedupe_key = _key(job, dedupe_fields)
        first = canonical.get(job.dedupe_key) or session.scalar(
            select(Job.id)
            .where(Job.dedupe_key == job.dedupe_key, Job.duplicate_of.is_(None))
            .order_by(Job.id)
            .limit(1)
        )
        job.duplicate_of = first
        session.add(job)
        session.flush()
        if first is None:
            canonical[job.dedupe_key] = job.id
            new += 1
        else:
            duplicate += 1
    return StoreResult(new=new, duplicate=duplicate, seen=len(existing), filled=filled)
