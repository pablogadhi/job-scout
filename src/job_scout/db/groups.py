"""Duplicate groups: a canonical job plus every posting linked to it via `duplicate_of`.

The same job is often posted on several boards, or once per country on one board. Judging
only the first copy could drop or misjudge a job whose other copies are eligible, so the
pre-filter and the scorer look at the whole group.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from job_scout.db.models import Job

_CHUNK = 500  # stay well under SQLite's bound-parameter limit


@dataclass(frozen=True)
class Group:
    canonical: Job
    copies: tuple[Job, ...]  # linked duplicates, oldest first

    @property
    def jobs(self) -> tuple[Job, ...]:
        return (self.canonical, *self.copies)

    def combined_locations(self) -> list[str]:
        """Each distinct location statement in the group, labelled with its source.

        e.g. ["4dayweek: Germany (remote)", "4dayweek: Mexico (remote)", "weworkremotely: ..."]
        """
        seen: dict[str, None] = {}
        for job in self.jobs:
            if job.location_raw:
                seen.setdefault(f"{job.source_id}: {job.location_raw}", None)
        return list(seen)


def load_groups(session: Session, canonical_ids: Iterable[int]) -> list[Group]:
    """Groups for the given canonical job ids, in the order given."""
    ids = list(dict.fromkeys(canonical_ids))
    if not ids:
        return []
    rows: list[Job] = []
    for start in range(0, len(ids), _CHUNK):
        part = ids[start : start + _CHUNK]
        rows.extend(
            session.scalars(
                select(Job)
                .where(or_(Job.id.in_(part), Job.duplicate_of.in_(part)))
                .order_by(Job.id)
            )
        )
    wanted = set(ids)
    canonical = {job.id: job for job in rows if job.id in wanted}
    copies: dict[int, list[Job]] = defaultdict(list)
    for job in rows:
        if job.duplicate_of is not None:
            copies[job.duplicate_of].append(job)
    return [Group(canonical[i], tuple(copies[i])) for i in ids if i in canonical]


def group_of(session: Session, job_id: int) -> Group | None:
    """The group a job belongs to, whether it is the canonical job or a copy."""
    job = session.get(Job, job_id)
    if job is None:
        return None
    groups = load_groups(session, [job.duplicate_of or job.id])
    return groups[0] if groups else None
