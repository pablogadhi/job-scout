"""Scored job groups for the digest and the backlog report, sorted into sections (PLAN.md §8).

Every job's score counts, whichever profile.md it was scored under. Sections, in order:
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
from functools import cached_property
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from job_scout.config import AppConfig
from job_scout.config.profile import COMPUTED_KEYS, CORE_OUTPUT_KEYS, Profile
from job_scout.config.secrets import secret
from job_scout.config.settings import Digest as DigestSettings
from job_scout.db.groups import Group, load_groups
from job_scout.db.models import Digest, Score, ScoreStatus, Source
from job_scout.fetch.normalize import normalize_company
from job_scout.fetch.runner import NOT_YET
from job_scout.scoring.points import Breakdown, Item, Part
from job_scout.scoring.prompt import salary_line
from job_scout.scoring.schema import CoreResult

# Fields the report templates show by name: the core ones and these, from the first
# profile. Any other field a profile declares is listed generically ("Travel required: …")
# and gets its own CSV column.
NAMED_FIELDS = (
    CORE_OUTPUT_KEYS
    | COMPUTED_KEYS
    | {
        "job_id",
        "seniority_level",  # in the score's breakdown
        "matched_skills",  # results scored before points
        "gaps",
        "location_notes",
        "salary_usd_range",
        "salary_notes",
        "role_category",
        "seniority",
        "skill_fit",
        "seniority_fit",
        "four_day_week",
        "wlb_positive",
        "wlb_negative",
        "employment_type",
    }
)


def extra_output_fields(profile: Profile) -> list[str]:
    """The profile's fields the reports have no named place for, in the profile's order."""
    return [f.name for f in profile.output_fields if f.name not in NAMED_FIELDS]


def _objects(value: Any) -> bool:
    return isinstance(value, list) and any(isinstance(v, dict) for v in value)


def item_mark(item: Item) -> str:
    """✓ for a requirement that earned points, ✗ for one that didn't."""
    return "✓" if item.points > 0 else "✗"


def field_label(name: str) -> str:
    return name.replace("_", " ").capitalize()


def field_text(value: Any) -> str | None:
    """A field's value as report text; None when there's nothing to show."""
    if value is None or value == "" or value == []:
        return None
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ("; " if any(isinstance(v, dict) for v in value) else ", ").join(
            _object_text(v) if isinstance(v, dict) else str(v) for v in value
        )
    return str(value)


def _object_text(item: dict[str, Any]) -> str:
    """One object of a list of objects: its first value, then the others in brackets
    (a true flag by its name, a false one left out)."""
    values = list(item.items())
    if not values:
        return ""
    rest = [k if v is True else str(v) for k, v in values[1:] if v is not False and v != ""]
    head = str(values[0][1])
    return f"{head} ({', '.join(rest)})" if rest else head


@dataclass(frozen=True)
class Listing:
    source_id: str
    url: str
    location: str | None


@dataclass(frozen=True)
class ReportItem:
    group: Group
    score: Score
    result: CoreResult
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
        return self.score.overall_score or 0

    @property
    def disqualified(self) -> bool:
        return bool(self.score.disqualified)

    @cached_property
    def breakdown(self) -> Breakdown | None:
        """How the score was computed; None for a result scored before points."""
        return Breakdown.from_json(self.score.breakdown) if self.score.breakdown else None

    @property
    def disqualify_reason(self) -> str | None:
        if self.breakdown is not None:
            return self.breakdown.disqualify_reason
        return (self.result.model_extra or {}).get("disqualify_reason")

    @property
    def badges(self) -> list[str]:
        if self.breakdown is not None:
            return list(self.breakdown.badges)
        return (
            ["4-day week"] if (self.result.model_extra or {}).get("four_day_week") == "yes" else []
        )

    @property
    def requirement_parts(self) -> list[Part]:
        """Components scored item by item against a maximum (the skills)."""
        parts = self.breakdown.parts if self.breakdown else ()
        return [p for p in parts if p.max is not None and p.items]

    @property
    def signal_parts(self) -> list[Part]:
        """Components that add or subtract per item (the signals)."""
        parts = self.breakdown.parts if self.breakdown else ()
        return [p for p in parts if p.max is None and p.items]

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

    @property
    def other_fields(self) -> list[tuple[str, str]]:
        """(label, text) for the result's fields the templates don't name, in its order."""
        extra = self.result.model_extra or {}
        return [
            (field_label(name), text)
            for name, value in extra.items()
            if name not in NAMED_FIELDS
            and not (self.breakdown and _objects(value))  # shown as requirements and signals
            and (text := field_text(value)) is not None
        ]

    @property
    def role_parts(self) -> list[str]:
        """The role row's pieces a result has, for profiles without the first profile's
        full set (role, seniority, skill and seniority fit, employment type)."""
        fields = self.result.model_extra or {}
        parts = [str(fields[k]) for k in ("role_category", "seniority") if fields.get(k)]
        parts += [
            f"{label} {fields[k]}/5"
            for k, label in (("skill_fit", "skill fit"), ("seniority_fit", "seniority fit"))
            if fields.get(k) is not None
        ]
        parts.append(f"remote: {self.result.remote}")
        if fields.get("employment_type"):
            parts.append(str(fields["employment_type"]))
        return parts


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
    session: Session, watchlist: set[str], exclude: Iterable[int] = ()
) -> list[ReportItem]:
    """Every scored group, best first (ties: newest posting first)."""
    skip = set(exclude)
    scores = [
        s
        for s in session.scalars(select(Score).where(Score.status == ScoreStatus.SCORED))
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
                result=CoreResult.model_validate(score.result),
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
            and not item.disqualified
            and item.overall >= settings.watchlist_min_score
        ):
            sections.watchlist.append(item)
        elif not item.disqualified and item.overall >= settings.min_score:
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
                    if source.auth_env and secret(environ, source.auth_env) is None
                    else None
                ),
            )
        )
    return health
