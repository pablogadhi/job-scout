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
    ERROR = "error"


class SalaryPeriod(enum.StrEnum):
    HOUR = "hour"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"


class AtsStatus(enum.StrEnum):
    OK = "ok"
    UNKNOWN_ATS = "unknown_ats"


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
        AtsStatus: _enum(AtsStatus),
        AtsSeed: _enum(AtsSeed),
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
    __tablename__ = "prefilter_results"

    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    passed: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str | None] = mapped_column(Text)


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
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    cost_usd_estimate: Mapped[float | None] = mapped_column(Float)
    duration_ms: Mapped[int | None]
    error: Mapped[str | None] = mapped_column(Text)
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
    call_id: Mapped[int | None] = mapped_column(ForeignKey("scoring_calls.id"), index=True)
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    cost_usd_estimate: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Digest(Base):
    __tablename__ = "digests"

    id: Mapped[int] = mapped_column(primary_key=True)
    sent_at: Mapped[datetime] = mapped_column(default=utcnow)
    job_ids: Mapped[list[int]] = mapped_column(default=list)


class AtsCompany(Base):
    __tablename__ = "ats_companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    company: Mapped[str] = mapped_column(String(255), unique=True)
    careers_url: Mapped[str | None] = mapped_column(Text)
    ats: Mapped[str | None] = mapped_column(String(32))
    token: Mapped[str | None] = mapped_column(String(255))
    region: Mapped[str | None] = mapped_column(String(64))
    detected_at: Mapped[datetime | None]
    status: Mapped[AtsStatus]
    source: Mapped[AtsSeed]
