"""Run fetchers source by source. One broken source never stops the others."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from job_scout.ats.store import boards as ats_boards
from job_scout.ats.store import record_polls
from job_scout.config import AppConfig, SourceConfig
from job_scout.db.models import Source, SourceRun, SourceRunKind, SourceRunStatus
from job_scout.db.types import utcnow
from job_scout.fetch.base import FetchContext
from job_scout.fetch.http import FetchError, HttpClient, RequestBudget
from job_scout.fetch.sources import FETCHERS
from job_scout.fetch.store import store_postings

log = logging.getLogger(__name__)

NOT_YET = {"scrape": "scraper (Phase 7)"}


@dataclass
class SourceOutcome:
    source_id: str
    status: Literal["ok", "failed", "skipped"]
    reason: str | None = None
    fetched: int = 0
    new: int = 0
    duplicate: int = 0
    seen: int = 0
    skipped_items: int = 0
    requests: int = 0


def select_sources(
    config: AppConfig, source_ids: Sequence[str] = (), tier: int | None = None
) -> list[SourceConfig]:
    """Explicit --source ids win (even if not enabled); otherwise enabled sources by tier."""
    if source_ids:
        return [config.sources.get(sid) for sid in source_ids]
    return config.sources.enabled(tier)


def requests_last_24h(session: Session, source_id: str, now: datetime) -> int:
    return session.scalar(
        select(func.coalesce(func.sum(SourceRun.requests_made), 0)).where(
            SourceRun.source_id == source_id, SourceRun.started_at >= now - timedelta(hours=24)
        )
    )


def _skip_reason(
    session: Session,
    source: SourceConfig,
    state: Source,
    *,
    config: AppConfig,
    env: Mapping[str, str],
    now: datetime,
    respect_interval: bool,
) -> str | None:
    if source.method in NOT_YET:
        return f"{NOT_YET[source.method]} not implemented yet"
    if source.id not in FETCHERS:
        return "no fetcher registered"
    if source.auth_env and not env.get(source.auth_env):
        return f"{source.auth_env} is not set"
    if source.method == "ats_seed" and not ats_boards(session, config.sources.ats_platforms):
        return "no company boards yet: run `job-scout ats detect`"
    if respect_interval and state.last_run_at is not None:
        interval = timedelta(hours=config.sources.defaults.min_interval_hours)
        if now - state.last_run_at < interval:
            ago = (now - state.last_run_at).total_seconds() / 3600
            return (
                f"ran {ago:.1f}h ago (min interval {interval.total_seconds() / 3600:g}h; --force)"
            )
    if source.max_requests_per_day:
        used = requests_last_24h(session, source.id, now)
        if source.max_requests_per_day - used < len(source.endpoints):
            return f"request budget: {used} of {source.max_requests_per_day} used in the last 24h"
    return None


def _budget(session: Session, source: SourceConfig, now: datetime) -> RequestBudget:
    if not source.max_requests_per_day:
        return RequestBudget()
    return RequestBudget(
        remaining=source.max_requests_per_day - requests_last_24h(session, source.id, now)
    )


def run_fetch(
    config: AppConfig,
    session_factory: sessionmaker[Session],
    http: HttpClient,
    *,
    source_ids: Sequence[str] = (),
    tier: int | None = None,
    force: bool = False,
    probe: bool = False,
    env: Mapping[str, str] | None = None,
) -> list[SourceOutcome]:
    """Fetch each selected source and store its postings (or, with probe, only count them)."""
    env = env if env is not None else {}
    outcomes: list[SourceOutcome] = []
    for source in select_sources(config, source_ids, tier):
        outcomes.append(
            _run_one(config, session_factory, http, source, force=force, probe=probe, env=env)
        )
    return outcomes


def _run_one(
    config: AppConfig,
    session_factory: sessionmaker[Session],
    http: HttpClient,
    source: SourceConfig,
    *,
    force: bool,
    probe: bool,
    env: Mapping[str, str],
) -> SourceOutcome:
    now = utcnow()
    with session_factory.begin() as session:
        state = session.get(Source, source.id) or Source(id=source.id)
        session.add(state)
        reason = _skip_reason(
            session,
            source,
            state,
            config=config,
            env=env,
            now=now,
            respect_interval=not (force or probe),
        )
        if reason:
            log.info("%s: skipped: %s", source.id, reason)
            return SourceOutcome(source.id, "skipped", reason)
        budget = _budget(session, source, now)
        last_success_at = state.last_success_at
        boards = (
            ats_boards(session, config.sources.ats_platforms) if source.method == "ats_seed" else []
        )

    ctx = FetchContext(
        source=source,
        http=http,
        budget=budget,
        now=now,
        last_success_at=last_success_at,
        env=env,
        boards=boards,
    )
    outcome = SourceOutcome(source.id, "ok")
    postings = []
    try:
        postings = FETCHERS[source.id].fetch(ctx)
    except FetchError as exc:
        outcome.status, outcome.reason = "failed", str(exc)
    except Exception as exc:  # a bug in one mapper must not stop the other sources
        log.exception("%s: unexpected error", source.id)
        outcome.status, outcome.reason = "failed", f"{type(exc).__name__}: {exc}"
    outcome.fetched, outcome.skipped_items, outcome.requests = (
        len(postings),
        ctx.skipped_items,
        budget.used,
    )
    failed_boards = sum(e is not None for e in ctx.board_results.values())
    if outcome.status == "ok" and failed_boards:
        outcome.reason = f"{failed_boards} of {len(ctx.boards)} company boards failed"

    with session_factory.begin() as session:
        run = SourceRun(
            source_id=source.id,
            kind=SourceRunKind.PROBE if probe else SourceRunKind.FETCH,
            status=SourceRunStatus.OK if outcome.status == "ok" else SourceRunStatus.FAILED,
            started_at=now,
            requests_made=budget.used,
            items_fetched=len(postings),
            items_skipped=ctx.skipped_items,
            error=outcome.reason,
        )
        if not probe:
            record_polls(session, ctx.board_results, now)
            state = session.get_one(Source, source.id)
            state.last_run_at = now
            if outcome.status == "ok":
                stored = store_postings(
                    session, source.id, postings, config.filters.dedupe.key, now
                )
                outcome.new, outcome.duplicate, outcome.seen = (
                    stored.new,
                    stored.duplicate,
                    stored.seen,
                )
                run.items_new, run.items_duplicate = stored.new, stored.duplicate
                state.last_success_at = now
                state.last_item_count = len(postings)
                state.consecutive_failures = 0
                state.consecutive_zero_runs = 0 if postings else state.consecutive_zero_runs + 1
            else:
                state.consecutive_failures += 1
        run.finished_at = utcnow()
        session.add(run)

    log.info(
        "%s: %s fetched=%d new=%d duplicate=%d seen=%d requests=%d%s",
        source.id,
        outcome.status,
        outcome.fetched,
        outcome.new,
        outcome.duplicate,
        outcome.seen,
        outcome.requests,
        f" ({outcome.reason})" if outcome.reason else "",
    )
    return outcome
