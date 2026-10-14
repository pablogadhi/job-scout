"""`run` (the daily timer) and `backlog`: several stages in one command."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

import typer

from job_scout.cli import ats, digest, scoring, sources
from job_scout.cli.common import (
    BackendOpt,
    DryRunOpt,
    MaxCallsOpt,
    MaxInputTokensOpt,
    ModelOpt,
    PostingsPerCallOpt,
    console,
    fail,
    load,
    open_db,
    scoring_overrides,
)
from job_scout.config import AppConfig
from job_scout.db.models import ScoringMode
from job_scout.db.types import utcnow
from job_scout.prefilter_run import run_prefilter
from job_scout.report.build import make_backlog_report
from job_scout.scoring.runner import RunReport

log = logging.getLogger(__name__)


def run(ctx: typer.Context) -> None:
    """Daily pipeline: fetch, filter, score (daily mode), digest. What the timer calls.

    Every stage runs even if an earlier one failed (the digest still goes out); the exit code
    is 1 if any stage failed, so the server's health check notices.
    """
    config = load(ctx)
    session_factory = open_db(config)
    failures: list[str] = []
    _fetch_and_filter(config, failures)
    _stage(
        "score",
        failures,
        lambda: scoring.run_scoring(config, session_factory, mode=ScoringMode.DAILY, command="run"),
    )
    _stage("digest", failures, lambda: digest.send_digest(config, session_factory))
    if failures:
        raise fail(", ".join(failures), 1, label="run finished with failures")


def backlog(
    ctx: typer.Context,
    backend: BackendOpt = None,
    model: ModelOpt = None,
    postings_per_call: PostingsPerCallOpt = None,
    max_calls: MaxCallsOpt = None,
    max_input_tokens: MaxInputTokensOpt = None,
    dry_run: DryRunOpt = False,
) -> None:
    """Fetch everything, score in backlog mode, write a ranked report. Re-run until nothing
    is pending; each run's report covers everything scored so far.

    --dry-run only plans the scoring (no fetch, no calls, no report).
    """
    config = load(ctx, scoring_overrides(backend, model))
    session_factory = open_db(config)

    def score_backlog() -> RunReport:
        return scoring.run_scoring(
            config,
            session_factory,
            mode=ScoringMode.BACKLOG,
            command="backlog",
            postings_per_call=postings_per_call,
            max_calls=max_calls,
            max_input_tokens=max_input_tokens,
            dry_run=dry_run,
        )

    if dry_run:
        score_backlog()
        return
    failures: list[str] = []
    _fetch_and_filter(config, failures)
    report = _stage("score", failures, score_backlog)
    outcome = make_backlog_report(config, session_factory, now=utcnow(), environ=os.environ)
    s = outcome.sections
    console.print(
        f"backlog report: {len(s.watchlist)} watchlist, {len(s.top)} top, "
        f"{len(s.unclear)} unclear location, {len(s.below)} below threshold -> {outcome.path}"
    )
    if report is not None and report.pending_left:
        console.print(f"{report.pending_left} job groups still pending: run `backlog` again")
    if failures:
        raise fail(", ".join(failures), 1, label="backlog finished with failures")


def _stage(name: str, failures: list[str], step: Callable[[], Any]) -> Any:
    """Run one pipeline stage; a failure is logged and recorded, and the next stage still runs."""
    try:
        return step()
    except typer.Exit as exc:
        if exc.exit_code:
            failures.append(name)
    except Exception:
        log.exception("%s failed", name)
        failures.append(name)
    return None


def _fetch_and_filter(config: AppConfig, failures: list[str]) -> None:
    def fetch_all() -> None:
        outcomes = sources.fetch_sources(config, source_ids=[], tier=None)
        sources.print_outcomes(outcomes, "Fetch results")
        sources.exit_if_all_failed(outcomes)

    def filter_new() -> None:
        result = run_prefilter(config, open_db(config))
        console.print(
            f"pre-filter: evaluated {result.evaluated} job groups, {result.passed} passed"
        )

    _stage("ats", failures, lambda: ats.upkeep(config))
    _stage("fetch", failures, fetch_all)
    _stage("filter", failures, filter_new)
