"""Stored data (PLAN.md §6). Kept separate from the Pydantic models used for input."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Enum,
    Float,
    ForeignKey,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from job_scout.db.types import UTCDateTime, utcnow

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Backend(enum.StrEnum):
    CLI = "cli"
    API = "api"


class ScoringMode(enum.StrEnum):
    DAILY = "daily"
    BACKLOG = "backlog"


class ScoreStatus(enum.StrEnum):
    PENDING = "pending"
    SCORED = "scored"
    SCORE_FAILED = "score_failed"


class CallStatus(enum.StrEnum):
    RUNNING = "running"
    SUBMITTED = "submitted"  # Batches API: waiting on the batch to end
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    USAGE_LIMIT = "usage_limit"


class StopReason(enum.StrEnum):
    DONE = "done"
    MAX_CALLS = "max_calls"
    USAGE_LIMIT = "usage_limit"
    SPEND_LIMIT = "spend_limit"
    BATCH_PENDING = "batch_pending"  # a Batches API batch is still processing; next run resumes it
    ERROR = "error"


class SalaryPeriod(enum.StrEnum):
    HOUR = "hour"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"


class SourceRunStatus(enum.StrEnum):
    OK = "ok"
    FAILED = "failed"


class SourceRunKind(enum.StrEnum):
    FETCH = "fetch"  # postings stored, source health updated
    PROBE = "probe"  # `sources check --probe`: nothing stored, requests still count


class DigestKind(enum.StrEnum):
    DIGEST = "digest"  # the daily digest
    BACKLOG = "backlog"  # a backlog report; its jobs aren't repeated by later digests


class Vote(enum.StrEnum):
    UP = "up"
    DOWN = "down"


class AtsStatus(enum.StrEnum):
    PENDING = "pending"  # not detected yet
    OK = "ok"  # board found: polled daily
    UNKNOWN_ATS = "unknown_ats"  # no supported board found: listed for manual review
    NOT_IN_SEED = "not_in_seed"  # dropped from the seed list (or its filters): not polled


class AtsMethod(enum.StrEnum):
    """How the board was found (sources.yaml ATS detection order)."""

    CAREERS_URL = "careers_url"  # the careers URL is the board itself
    CAREERS_PAGE = "careers_page"  # the careers page links to or embeds the board
    PROBE = "probe"  # the company slug answered on the ATS API with jobs
    WATCHLIST = "watchlist"  # given in sources.yaml


class AtsSeed(enum.StrEnum):
    REMOTEINTECH = "remoteintech"
    WATCHLIST = "watchlist"


def _enum(cls: type[enum.StrEnum]) -> Enum:
    # VARCHAR with no CHECK constraint: portable to Postgres, and new values need no migration.
    return Enum(
        cls,
        native_enum=False,
        create_constraint=False,
        length=32,
        values_callable=lambda e: [m.value for m in e],
    )


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {
        datetime: UTCDateTime(),
        dict[str, Any]: JSON(),
        list[Any]: JSON(),
        list[str]: JSON(),
        list[int]: JSON(),
        Backend: _enum(Backend),
        ScoringMode: _enum(ScoringMode),
        ScoreStatus: _enum(ScoreStatus),
        CallStatus: _enum(CallStatus),
        StopReason: _enum(StopReason),
        SalaryPeriod: _enum(SalaryPeriod),
        SourceRunStatus: _enum(SourceRunStatus),
        SourceRunKind: _enum(SourceRunKind),
        AtsStatus: _enum(AtsStatus),
        DigestKind: _enum(DigestKind),
        AtsSeed: _enum(AtsSeed),
        AtsMethod: _enum(AtsMethod),
        Vote: _enum(Vote),
    }


class Source(Base):
    """Per-source run health. `id` matches the source id in sources.yaml."""

    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_run_at: Mapped[datetime | None]
    last_success_at: Mapped[datetime | None]
    last_item_count: Mapped[int | None]
    consecutive_failures: Mapped[int] = mapped_column(default=0, server_default="0")
    consecutive_zero_runs: Mapped[int] = mapped_column(default=0, server_default="0")


class SourceRun(Base):
    """One attempt at a source. Feeds request budgets (e.g. Remotive's 4/day) and health."""

    __tablename__ = "source_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    kind: Mapped[SourceRunKind] = mapped_column(default=SourceRunKind.FETCH)
    status: Mapped[SourceRunStatus]
    started_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    finished_at: Mapped[datetime | None]
    requests_made: Mapped[int] = mapped_column(default=0, server_default="0")
    items_fetched: Mapped[int] = mapped_column(default=0, server_default="0")
    items_new: Mapped[int] = mapped_column(default=0, server_default="0")
    items_duplicate: Mapped[int] = mapped_column(default=0, server_default="0")
    items_skipped: Mapped[int] = mapped_column(default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("source_id", "source_job_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    source_job_id: Mapped[str] = mapped_column(String(255))
    canonical_url: Mapped[str] = mapped_column(Text)
    apply_url: Mapped[str | None] = mapped_column(Text)
    company: Mapped[str | None] = mapped_column(String(255))
    company_normalized: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(500))
    title_normalized: Mapped[str] = mapped_column(String(500))
    location_raw: Mapped[str | None] = mapped_column(Text)
    remote_raw: Mapped[str | None] = mapped_column(Text)
    salary_min: Mapped[float | None] = mapped_column(Float)
    salary_max: Mapped[float | None] = mapped_column(Float)
    salary_currency: Mapped[str | None] = mapped_column(String(3))
    salary_period: Mapped[SalaryPeriod | None]
    description_text: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(default=list)
    posted_at: Mapped[datetime | None]
    first_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    raw: Mapped[dict[str, Any]]
    dedupe_key: Mapped[str] = mapped_column(String(64), index=True)
    duplicate_of: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), index=True)


class PrefilterResult(Base):
    """Verdict for a canonical job's whole duplicate group (copies have no row)."""

    __tablename__ = "prefilter_results"

    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    passed: Mapped[bool] = mapped_column(Boolean, index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    evaluated_at: Mapped[datetime] = mapped_column(default=utcnow)
    # Prefilter.fingerprint at evaluation time: a mismatch means re-evaluate.
    fingerprint: Mapped[str] = mapped_column(String(64))
    group_size: Mapped[int] = mapped_column(default=1, server_default="1")


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    command: Mapped[str] = mapped_column(String(32))
    mode: Mapped[ScoringMode | None]
    backend: Mapped[Backend | None]
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None]
    calls_made: Mapped[int] = mapped_column(default=0, server_default="0")
    postings_scored: Mapped[int] = mapped_column(default=0, server_default="0")
    postings_left_pending: Mapped[int] = mapped_column(default=0, server_default="0")
    stop_reason: Mapped[StopReason | None]


class ScoringCall(Base):
    __tablename__ = "scoring_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    backend: Mapped[Backend]
    model: Mapped[str] = mapped_column(String(100))
    mode: Mapped[ScoringMode]
    postings_count: Mapped[int]
    status: Mapped[CallStatus] = mapped_column(default=CallStatus.RUNNING)
    exit_code: Mapped[int | None]  # cli backend
    http_status: Mapped[int | None]  # api backend
    api_batch_id: Mapped[str | None] = mapped_column(String(100), index=True)
    # Batches API: custom_id -> job ids, so results can be ingested after a restart.
    request_map: Mapped[dict[str, Any] | None]
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    cost_usd_estimate: Mapped[float | None] = mapped_column(Float)
    duration_ms: Mapped[int | None]
    error: Mapped[str | None] = mapped_column(Text)
    # Jobs whose feedback was in the prompt as examples (None: no examples).
    feedback_job_ids: Mapped[list[int] | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Score(Base):
    __tablename__ = "scores"
    __table_args__ = (UniqueConstraint("job_id", "profile_hash"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    profile_hash: Mapped[str] = mapped_column(String(64))
    backend: Mapped[Backend | None]  # set once a backend has scored it
    model: Mapped[str | None] = mapped_column(String(100))
    result: Mapped[dict[str, Any] | None]
    overall_score: Mapped[int | None] = mapped_column(index=True)
    disqualified: Mapped[bool | None] = mapped_column(Boolean)
    status: Mapped[ScoreStatus] = mapped_column(default=ScoreStatus.PENDING, index=True)
    attempts: Mapped[int] = mapped_column(default=0, server_default="0")  # failed calls so far
    scored_at: Mapped[datetime | None]
    call_id: Mapped[int | None] = mapped_column(ForeignKey("scoring_calls.id"), index=True)
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    cost_usd_estimate: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Digest(Base):
    """A delivered digest or backlog report. `job_ids` (canonical jobs) are never reported
    again by a later digest. A digest whose email failed isn't recorded, so its jobs carry
    over to the next one."""

    __tablename__ = "digests"

    id: Mapped[int] = mapped_column(primary_key=True)
    sent_at: Mapped[datetime] = mapped_column(default=utcnow)
    job_ids: Mapped[list[int]] = mapped_column(default=list)
    kind: Mapped[DigestKind] = mapped_column(default=DigestKind.DIGEST, server_default="digest")
    report_path: Mapped[str | None] = mapped_column(Text)
    emailed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")


class AtsCompany(Base):
    """A company whose own job board (ATS) is polled: from the remoteintech seed or the
    watchlist. One row per slug; a watchlist entry overrides the seed's detection."""

    __tablename__ = "ats_companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(255), unique=True)
    company: Mapped[str] = mapped_column(String(255))
    website: Mapped[str | None] = mapped_column(Text)
    careers_url: Mapped[str | None] = mapped_column(Text)
    region: Mapped[str | None] = mapped_column(String(64))
    remote_policy: Mapped[str | None] = mapped_column(String(64))
    ats: Mapped[str | None] = mapped_column(String(32))
    token: Mapped[str | None] = mapped_column(String(255))
    method: Mapped[AtsMethod | None]
    status: Mapped[AtsStatus]
    source: Mapped[AtsSeed]
    detected_at: Mapped[datetime | None]  # last detection attempt (re-detected monthly)
    # Inconclusive detections in a row (ats.store.MAX_DETECTION_ATTEMPTS); detected_at stays
    # unset meanwhile, so the company is picked up again next run.
    detection_retries: Mapped[int] = mapped_column(default=0, server_default="0")
    seen_in_seed_at: Mapped[datetime | None]  # last seed download that listed it
    last_polled_at: Mapped[datetime | None]
    last_poll_error: Mapped[str | None] = mapped_column(Text)


class Feedback(Base):
    """Your thumbs up/down on a job group (from the digest's feedback page). One row per
    canonical job: a new vote replaces the old one. The score you were reacting to is copied
    in, so the judgment stays readable after a rescore or a profile change."""

    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), unique=True)
    vote: Mapped[Vote]
    reason: Mapped[str | None] = mapped_column(Text)
    profile_hash: Mapped[str | None] = mapped_column(String(64))
    overall_score: Mapped[int | None]
    disqualified: Mapped[bool | None] = mapped_column(Boolean)
    summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
