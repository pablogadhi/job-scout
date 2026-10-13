"""Scored job groups for the digest and the backlog report, sorted into sections (PLAN.md §8).

Only scores under the current profile (profile_hash) count. Sections, in order:
- watchlist: a listing's company is on the watchlist, not disqualified, score >= watchlist_min
- top: score >= digest.min_score and location eligibility pass (or fail, which can't reach
  min_score without being disqualified)
- unclear: score >= digest.min_score but location eligibility unclear
- below: everything else that was scored (backlog report only)
A job appears in one section only. The digest skips jobs an earlier digest or backlog report
already reported.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from job_scout.config import AppConfig
from job_scout.config.settings import Digest as DigestSettings
from job_scout.db.models import Digest, Score, ScoreStatus, Source
from job_scout.fetch.normalize import normalize_company
from job_scout.fetch.runner import NOT_YET
from job_scout.groups import Group, load_groups
from job_scout.scoring.prompt import salary_line
from job_scout.scoring.schema import ScoreResult


@dataclass(frozen=True)
class Listing:
    source_id: str
    url: str
    location: str | None


@dataclass(frozen=True)
class ReportItem:
    group: Group
    score: Score
    result: ScoreResult
    watchlist: bool

    @property
    def job_id(self) -> int:
        return self.group.canonical.id

    @property
    def title(self) -> str:
        return self.group.canonical.title

    @property
    def company(self) -> str:
        return self.group.canonical.company or "unknown company"

    @property
    def overall(self) -> int:
        return self.result.overall_score

    @property
    def apply_url(self) -> str:
        job = self.group.canonical
        return job.apply_url or job.canonical_url

    @property
    def listings(self) -> list[Listing]:
        return [Listing(j.source_id, j.canonical_url, j.location_raw) for j in self.group.jobs]

    @property
    def salary(self) -> str | None:
        return salary_line(self.group)

    @property
    def posted_at(self) -> datetime | None:
        return self.group.canonical.posted_at


@dataclass
class Sections:
    watchlist: list[ReportItem] = field(default_factory=list)
    top: list[ReportItem] = field(default_factory=list)
    unclear: list[ReportItem] = field(default_factory=list)
    below: list[ReportItem] = field(default_factory=list)

    @property
    def reported(self) -> list[ReportItem]:
        """What counts as reported (recorded in `digests`): not the below-threshold list."""
        return [*self.watchlist, *self.top, *self.unclear]

    @property
    def job_ids(self) -> list[int]:
        return [item.job_id for item in self.reported]

    def __len__(self) -> int:
        return len(self.reported)


def watchlist_names(config: AppConfig) -> set[str]:
    return {name for w in config.sources.watchlist if (name := normalize_company(w.company))}


def scored_items(
    session: Session, profile_hash: str, watchlist: set[str], exclude: Iterable[int] = ()
) -> list[ReportItem]:
    """Every scored group under this profile, best first (ties: newest posting first)."""
    skip = set(exclude)
    scores = [
        s
        for s in session.scalars(
            select(Score).where(
                Score.profile_hash == profile_hash, Score.status == ScoreStatus.SCORED
            )
        )
        if s.job_id not in skip and s.result is not None
    ]
    groups = {g.canonical.id: g for g in load_groups(session, [s.job_id for s in scores])}
    items = []
    for score in scores:
        group = groups.get(score.job_id)
        if group is None:  # defensive: the scored job is gone or no longer canonical
            continue
        items.append(
            ReportItem(
                group=group,
                score=score,
                result=ScoreResult.model_validate(score.result),
                watchlist=any(j.company_normalized in watchlist for j in group.jobs),
            )
        )
    items.sort(key=lambda i: (-i.overall, -(i.posted_at.timestamp() if i.posted_at else 0)))
    return items


def build_sections(
    items: Sequence[ReportItem], settings: DigestSettings, *, include_below: bool
) -> Sections:
    sections = Sections()
    for item in items:
        result = item.result
        if (
            item.watchlist
            and not result.disqualified
            and item.overall >= settings.watchlist_min_score
        ):
            sections.watchlist.append(item)
        elif not result.disqualified and item.overall >= settings.min_score:
            if result.location_eligibility == "unclear":
                sections.unclear.append(item)
            else:
                sections.top.append(item)
        elif include_below:
            sections.below.append(item)
    return sections


def reported_job_ids(session: Session) -> set[int]:
    """Canonical jobs any earlier digest or backlog report already showed."""
    ids: set[int] = set()
    for job_ids in session.scalars(select(Digest.job_ids)):
        ids.update(job_ids or [])
    return ids


def last_digest(session: Session) -> Digest | None:
    return session.scalars(select(Digest).order_by(Digest.id.desc()).limit(1)).one_or_none()


@dataclass(frozen=True)
class SourceHealth:
    source_id: str
    last_run_at: datetime | None
    last_item_count: int | None
    consecutive_failures: int
    consecutive_zero_runs: int
    missing_env: str | None = None  # the source's auth_env, when unset

    @property
    def problem(self) -> str | None:
        if self.missing_env:
            return f"skipped: {self.missing_env} not set"
        if self.last_run_at is None:
            return "never fetched"
        if self.consecutive_failures:
            return f"failed {self.consecutive_failures} run(s) in a row"
        if self.consecutive_zero_runs >= 3:
            return f"0 items {self.consecutive_zero_runs} runs in a row"
        return None


def source_health(
    session: Session, config: AppConfig, environ: Mapping[str, str]
) -> list[SourceHealth]:
    """Enabled, implemented sources with their fetch health (PLAN.md §8 footer)."""
    rows = {s.id: s for s in session.scalars(select(Source))}
    health = []
    for source in config.sources.sources:
        if not source.is_enabled or source.method in NOT_YET:
            continue
        row = rows.get(source.id)
        health.append(
            SourceHealth(
                source_id=source.id,
                last_run_at=row.last_run_at if row else None,
                last_item_count=row.last_item_count if row else None,
                consecutive_failures=row.consecutive_failures if row else 0,
                consecutive_zero_runs=row.consecutive_zero_runs if row else 0,
                missing_env=(
                    source.auth_env
                    if source.auth_env and not environ.get(source.auth_env)
                    else None
                ),
            )
        )
    return health
