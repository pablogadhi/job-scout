"""job-scout command line (PLAN.md §9).

Every command loads and validates all of config/ first, so a typo fails before any work.
Flags override env vars, which override settings.yaml.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from job_scout.ats.refresh import RefreshReport, seed_source
from job_scout.ats.refresh import refresh as refresh_ats
from job_scout.ats.store import counts as ats_counts
from job_scout.config import DEFAULT_CONFIG_DIR, AppConfig, ConfigError, SourceConfig, load_config
from job_scout.db import SchemaOutdated, check_schema, make_engine, make_session_factory
from job_scout.db.models import (
    AtsCompany,
    AtsStatus,
    Backend,
    Job,
    PrefilterResult,
    Score,
    ScoringMode,
    Source,
    SourceRun,
    SourceRunKind,
)
from job_scout.db.types import utcnow
from job_scout.feedback.examples import Example
from job_scout.feedback.links import SecretError, read_secret
from job_scout.feedback.report import feedback_stats
from job_scout.feedback.server import FeedbackApp, make_server
from job_scout.feedback.store import get_vote
from job_scout.fetch.http import FetchError, HttpClient, Recorder
from job_scout.fetch.runner import NOT_YET, SourceOutcome, run_fetch
from job_scout.groups import group_of
from job_scout.log import setup_logging
from job_scout.prefilter_run import (
    build_prefilter,
    drop_reasons,
    run_prefilter,
    sample,
    source_report,
)
from job_scout.report.build import DigestOutcome, make_backlog_report, make_digest
from job_scout.report.export import export_csv, snapshot_sqlite
from job_scout.report.failure import FailureReport, log_tail, send_failure_email
from job_scout.report.mailer import EmailError
from job_scout.scoring.backends import PlanOnlyScorer, make_scorer
from job_scout.scoring.calibrate import (
    ConfigSpec,
    Configuration,
    estimate_calls,
    run_calibration,
    sample_job_ids,
    save,
)
from job_scout.scoring.planner import Limits
from job_scout.scoring.queue import requeue_for_rescore
from job_scout.scoring.runner import RunOptions, RunReport, ScoringRunner
from job_scout.scoring.status import scoring_status

log = logging.getLogger(__name__)
console = Console()
err_console = Console(stderr=True)

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)
sources_app = typer.Typer(no_args_is_help=True, help="Inspect job sources.")
ats_app = typer.Typer(no_args_is_help=True, help="ATS company seed (remoteintech + watchlist).")
feedback_app = typer.Typer(no_args_is_help=True, help="Thumbs up/down from the digest.")
app.add_typer(sources_app, name="sources")
app.add_typer(ats_app, name="ats")
app.add_typer(feedback_app, name="feedback")


@dataclass
class State:
    config_dir: Path = DEFAULT_CONFIG_DIR
    overrides: dict[str, Any] = field(default_factory=dict)


def _load(ctx: typer.Context, overrides: dict[str, Any] | None = None) -> AppConfig:
    state: State = ctx.obj
    merged = {**state.overrides, **(overrides or {})}
    try:
        config = load_config(state.config_dir, merged)
    except ConfigError as exc:
        err_console.print(f"[red]config error:[/red] {escape(str(exc))}", soft_wrap=True)
        raise typer.Exit(2) from exc
    setup_logging(config.settings.log_level)
    log.debug("config loaded from %s (profile %s)", state.config_dir, config.profile.sha256[:12])
    return config


def _db(config: AppConfig) -> sessionmaker[Session]:
    engine = make_engine(config.settings.db_url)
    try:
        check_schema(engine)
    except (SchemaOutdated, OperationalError) as exc:
        err_console.print(f"[red]database error:[/red] {escape(str(exc))}", soft_wrap=True)
        raise typer.Exit(2) from exc
    return make_session_factory(engine)


def _scoring_overrides(backend: Backend | None, model: str | None) -> dict[str, Any]:
    scoring: dict[str, str] = {}
    if backend is not None:
        scoring["backend"] = backend.value
    if model is not None:
        scoring["model"] = model
    return {"scoring": scoring} if scoring else {}


@app.callback()
def main(
    ctx: typer.Context,
    config_dir: Annotated[
        Path, typer.Option(envvar="JOB_SCOUT_CONFIG_DIR", help="Directory with the config files.")
    ] = DEFAULT_CONFIG_DIR,
    log_level: Annotated[
        str | None, typer.Option(help="DEBUG, INFO, WARNING or ERROR. Env: JOB_SCOUT_LOG_LEVEL.")
    ] = None,
) -> None:
    overrides = {"log_level": log_level.upper()} if log_level else {}
    ctx.obj = State(config_dir=config_dir, overrides=overrides)


# Scoring flags shared by score, backlog and rescore.
BackendOpt = Annotated[Backend | None, typer.Option("--backend", help="Env: JOB_SCOUT_BACKEND.")]
ModelOpt = Annotated[
    str | None, typer.Option("--model", help="Full model ID. Env: JOB_SCOUT_MODEL.")
]
PostingsPerCallOpt = Annotated[int | None, typer.Option("--postings-per-call", min=1)]
MaxCallsOpt = Annotated[int | None, typer.Option("--max-calls", min=1)]
MaxInputTokensOpt = Annotated[int | None, typer.Option("--max-input-tokens", min=1)]
DryRunOpt = Annotated[bool, typer.Option("--dry-run", help="Plan only; no calls, no writes.")]


SourceOpt = Annotated[
    list[str] | None,
    typer.Option("--source", help="Only this source id (repeatable; works for disabled ones)."),
]
TierOpt = Annotated[int | None, typer.Option("--tier", min=1, max=3)]


def _print_outcomes(outcomes: list[SourceOutcome], title: str) -> None:
    table = Table(title=title, title_justify="left")
    table.add_column("source", no_wrap=True)
    table.add_column("status")
    for column in ("fetched", "new", "dup", "seen", "req"):
        table.add_column(column, justify="right")
    table.add_column("note")
    colors = {"ok": "green", "failed": "red", "skipped": "yellow"}
    for o in outcomes:
        table.add_row(
            o.source_id,
            f"[{colors[o.status]}]{o.status}[/]",
            *(
                str(n) if o.status != "skipped" else ""
                for n in (o.fetched, o.new, o.duplicate, o.seen, o.requests)
            ),
            escape(o.reason or (f"{o.skipped_items} items unmappable" if o.skipped_items else "")),
        )
    console.print(table)


def _fetch(
    config: AppConfig,
    *,
    source_ids: list[str],
    tier: int | None,
    force: bool = False,
    probe: bool = False,
    record: Path | None = None,
) -> list[SourceOutcome]:
    known = {s.id for s in config.sources.sources}
    if unknown := [sid for sid in source_ids if sid not in known]:
        err_console.print(f"[red]unknown source id:[/red] {', '.join(unknown)}")
        raise typer.Exit(2)
    session_factory = _db(config)
    recorder = Recorder(record) if record else None
    with HttpClient(config.sources.defaults, recorder=recorder) as http:
        return run_fetch(
            config,
            session_factory,
            http,
            source_ids=source_ids,
            tier=tier,
            force=force,
            probe=probe,
            env=os.environ,
        )


@app.command()
def fetch(
    ctx: typer.Context,
    source: SourceOpt = None,
    tier: TierOpt = None,
    force: Annotated[
        bool, typer.Option("--force", help="Ignore min_interval_hours (never request budgets).")
    ] = False,
    record: Annotated[
        Path | None,
        typer.Option(
            "--record",
            help="Save trimmed live responses under DIR (e.g. data/recordings) to study shapes.",
            hidden=True,
        ),
    ] = None,
) -> None:
    """Fetch postings from enabled sources and store new ones."""
    config = _load(ctx)
    tests_dir = (Path.cwd() / "tests").resolve()
    if record and record.resolve().is_relative_to(tests_dir):
        err_console.print(
            "[red]--record must not write under tests/:[/red] fixtures are synthetic "
            "(tests/fixtures/make_http_fixtures.py). Record to data/recordings instead."
        )
        raise typer.Exit(2)
    outcomes = _fetch(config, source_ids=source or [], tier=tier, force=force, record=record)
    _print_outcomes(outcomes, "Fetch results")
    attempted = [o for o in outcomes if o.status != "skipped"]
    if attempted and all(o.status == "failed" for o in attempted):
        raise typer.Exit(1)


def _limits(
    config: AppConfig,
    mode: ScoringMode,
    postings_per_call: int | None,
    max_calls: int | None,
    max_input_tokens: int | None,
) -> tuple[Limits, bool]:
    scoring = config.settings.scoring
    try:
        limits = scoring.limits(
            mode.value,
            postings_per_call=postings_per_call,
            max_calls=max_calls,
            max_input_tokens_per_call=max_input_tokens,
        )
    except ValueError as exc:
        err_console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(2) from exc
    return Limits.from_settings(limits), bool(getattr(limits, "use_batches_api", False))


def _run_scoring(
    config: AppConfig,
    session_factory: sessionmaker[Session],
    *,
    mode: ScoringMode,
    command: str,
    postings_per_call: int | None,
    max_calls: int | None,
    max_input_tokens: int | None,
    dry_run: bool,
) -> RunReport:
    scoring = config.settings.scoring
    limits, use_batches = _limits(config, mode, postings_per_call, max_calls, max_input_tokens)
    try:
        # A dry run only plans, so it needs no credentials.
        scorer = (
            PlanOnlyScorer(Backend(scoring.backend), scoring.model)
            if dry_run
            else make_scorer(scoring, scoring.backend, os.environ)
        )
    except RuntimeError as exc:
        err_console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(2) from exc
    runner = ScoringRunner(config, session_factory, scorer)
    report = runner.run(
        RunOptions(
            mode=mode,
            limits=limits,
            command=command,
            dry_run=dry_run,
            use_batches_api=use_batches,
        )
    )
    batching = "Batches API" if use_batches else f"up to {limits.postings_per_call}/call"
    console.print(
        f"backend {scoring.backend} ({scoring.model}), mode {mode.value}, {batching}, "
        f"max {limits.max_calls} calls"
    )
    new, requeued = report.enqueued
    if new or requeued:
        console.print(f"queued {new} new job groups, re-queued {requeued} that gained a listing")
    if dry_run:
        sizes = report.planned_calls
        cost = (
            f"; est. ${report.estimated_cost_usd:.2f}"
            if report.estimated_cost_usd is not None
            else ""
        )
        console.print(
            f"[bold]dry run[/bold]: {len(sizes)} calls planned "
            f"({_sizes_summary(sizes)} postings, {sum(sizes)} total){cost}; "
            f"{report.pending_left} pending"
        )
        return report
    stop = report.stop_reason.value if report.stop_reason else "?"
    color = "green" if stop == "done" else "yellow"
    console.print(
        f"run #{report.run_id}: {report.calls_made} calls, {report.scored} scored, "
        f"{report.failed} failed, {report.pending_left} still pending, "
        f"est. ${report.cost_usd:.4f}"
        f"{' (API-equivalent, not billed on Pro)' if scoring.backend == 'cli' else ''}; "
        f"stop reason: [{color}]{stop}[/{color}]"
    )
    if report.batch_id:
        console.print(f"batch {report.batch_id}")
    return report


def _sizes_summary(sizes: list[int]) -> str:
    """[20, 20, 20, 5] -> '3x20 + 1x5'."""
    if not sizes:
        return "none"
    runs: list[list[int]] = []
    for size in sizes:
        if runs and runs[-1][1] == size:
            runs[-1][0] += 1
        else:
            runs.append([1, size])
    return " + ".join(f"{count}x{size}" for count, size in runs)


@app.command()
def score(
    ctx: typer.Context,
    mode: Annotated[ScoringMode, typer.Option("--mode")] = ScoringMode.DAILY,
    backend: BackendOpt = None,
    model: ModelOpt = None,
    postings_per_call: PostingsPerCallOpt = None,
    max_calls: MaxCallsOpt = None,
    max_input_tokens: MaxInputTokensOpt = None,
    dry_run: DryRunOpt = False,
) -> None:
    """Score pending job groups with Claude."""
    config = _load(ctx, _scoring_overrides(backend, model))
    _run_scoring(
        config,
        _db(config),
        mode=mode,
        command="score",
        postings_per_call=postings_per_call,
        max_calls=max_calls,
        max_input_tokens=max_input_tokens,
        dry_run=dry_run,
    )


def _print_digest(config: AppConfig, outcome: DigestOutcome, dry_run: bool) -> None:
    s = outcome.sections
    console.print(
        f"digest: {len(s.watchlist)} watchlist, {len(s.top)} top, {len(s.unclear)} unclear "
        f"location -> {outcome.path}"
    )
    email = config.settings.digest.email
    if dry_run:
        console.print("[bold]dry run[/bold]: preview only; nothing emailed or recorded")
    elif outcome.emailed:
        console.print(f"emailed to {', '.join(email.recipients)}")
    elif outcome.emailed is False:
        err_console.print(
            f"[red]email failed:[/red] {escape(outcome.email_error or '?')}; not recorded, "
            "so the next digest carries these jobs again"
        )
    elif outcome.skipped_empty:
        console.print("nothing new: email skipped (digest.send_when_empty is false)")
    elif not email.enabled:
        console.print("email disabled (digest.email.enabled); HTML file only")


@app.command()
def digest(ctx: typer.Context, dry_run: DryRunOpt = False) -> None:
    """Write the digest of new matches (HTML) and email it when email is enabled.

    --dry-run writes reports/digest-preview.html and sends or records nothing.
    """
    config = _load(ctx)
    outcome = make_digest(config, _db(config), now=utcnow(), environ=os.environ, dry_run=dry_run)
    _print_digest(config, outcome, dry_run)
    if outcome.emailed is False:
        raise typer.Exit(1)


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
    def ats_upkeep() -> None:
        """Weekly seed download and capped board detection (settings.ats)."""
        source = seed_source(config)
        if source is None or not source.is_enabled:
            return
        with HttpClient(config.sources.defaults) as http:
            report = refresh_ats(
                config,
                _db(config),
                http,
                limit=config.settings.ats.max_detections_per_run,
            )
        _print_refresh(report)

    def fetch_all() -> None:
        outcomes = _fetch(config, source_ids=[], tier=None)
        _print_outcomes(outcomes, "Fetch results")
        attempted = [o for o in outcomes if o.status != "skipped"]
        if attempted and all(o.status == "failed" for o in attempted):
            raise typer.Exit(1)

    def filter_new() -> None:
        result = run_prefilter(config, _db(config))
        console.print(
            f"pre-filter: evaluated {result.evaluated} job groups, {result.passed} passed"
        )

    _stage("ats", failures, ats_upkeep)
    _stage("fetch", failures, fetch_all)
    _stage("filter", failures, filter_new)


@app.command()
def run(ctx: typer.Context) -> None:
    """Daily pipeline: fetch, filter, score (daily mode), digest. What the timer calls.

    Every stage runs even if an earlier one failed (the digest still goes out); the exit code
    is 1 if any stage failed, so the server's health check notices.
    """
    config = _load(ctx)
    session_factory = _db(config)
    failures: list[str] = []
    _fetch_and_filter(config, failures)
    _stage(
        "score",
        failures,
        lambda: _run_scoring(
            config,
            session_factory,
            mode=ScoringMode.DAILY,
            command="run",
            postings_per_call=None,
            max_calls=None,
            max_input_tokens=None,
            dry_run=False,
        ),
    )

    def send_digest() -> None:
        outcome = make_digest(config, session_factory, now=utcnow(), environ=os.environ)
        _print_digest(config, outcome, dry_run=False)
        if outcome.emailed is False:
            raise typer.Exit(1)

    _stage("digest", failures, send_digest)
    if failures:
        err_console.print(f"[red]run finished with failures:[/red] {', '.join(failures)}")
        raise typer.Exit(1)


@app.command()
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
    config = _load(ctx, _scoring_overrides(backend, model))
    session_factory = _db(config)

    def score_backlog() -> RunReport:
        return _run_scoring(
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
        err_console.print(f"[red]backlog finished with failures:[/red] {', '.join(failures)}")
        raise typer.Exit(1)


@app.command("notify-failure")
def notify_failure(
    ctx: typer.Context,
    unit: Annotated[str, typer.Option("--unit", help="The failed systemd unit.")] = "job-scout",
    result: Annotated[
        str | None, typer.Option("--result", help="systemd's MONITOR_SERVICE_RESULT.")
    ] = None,
    exit_status: Annotated[
        str | None, typer.Option("--exit-status", help="systemd's MONITOR_EXIT_STATUS.")
    ] = None,
) -> None:
    """Email a failed-run report; the run's log comes on stdin (systemd OnFailure= unit)."""
    config = _load(ctx)
    log = "" if sys.stdin.isatty() else sys.stdin.read()
    report = FailureReport(unit, result or None, exit_status or None, log_tail(log))
    now = datetime.now(ZoneInfo(config.settings.schedule.timezone))
    try:
        send_failure_email(config.settings.digest.email, report, os.environ, now)
    except EmailError as exc:
        err_console.print(f"[red]could not send the failure email:[/red] {escape(str(exc))}")
        err_console.print(report.text(now), markup=False)
        raise typer.Exit(1) from exc
    console.print(f"failure report emailed: {report.subject}")


@app.command()
def export(
    ctx: typer.Context,
    out: Annotated[
        Path | None, typer.Option("--out", help="Directory (default: paths.exports).")
    ] = None,
    sqlite: Annotated[
        bool, typer.Option("--sqlite", help="Also write a consistent snapshot of the database.")
    ] = False,
) -> None:
    """Write every posting to CSV (verdict and current score included) for skimming."""
    config = _load(ctx)
    directory = out or config.settings.paths.exports
    stamp = f"{utcnow():%Y%m%dT%H%M%SZ}"
    with _db(config)() as session:
        path = directory / f"jobs-{stamp}.csv"
        rows = export_csv(session, config.profile.sha256, path)
    console.print(f"{rows} postings -> {path}")
    if sqlite:
        try:
            snapshot = snapshot_sqlite(config.settings.db_url, directory / f"job-scout-{stamp}.db")
        except ValueError as exc:
            err_console.print(f"[red]{escape(str(exc))}[/red]")
            raise typer.Exit(2) from exc
        console.print(f"database snapshot -> {snapshot}")


@app.command()
def rescore(
    ctx: typer.Context,
    top: Annotated[int | None, typer.Option("--top", min=1, help="Top N by best score.")] = None,
    since: Annotated[
        datetime | None, typer.Option("--since", formats=["%Y-%m-%d"], help="First seen on/after.")
    ] = None,
    mode: Annotated[ScoringMode, typer.Option("--mode")] = ScoringMode.DAILY,
    backend: BackendOpt = None,
    model: ModelOpt = None,
    postings_per_call: PostingsPerCallOpt = None,
    max_calls: MaxCallsOpt = None,
    max_input_tokens: MaxInputTokensOpt = None,
    dry_run: DryRunOpt = False,
) -> None:
    """Re-score top or recent jobs under the current profile (after editing profile.md)."""
    if top is None and since is None:
        err_console.print("[red]give --top N and/or --since YYYY-MM-DD[/red]")
        raise typer.Exit(2)
    config = _load(ctx, _scoring_overrides(backend, model))
    session_factory = _db(config)
    with session_factory.begin() as session:
        since_utc = (
            since.replace(tzinfo=ZoneInfo(config.settings.schedule.timezone)) if since else None
        )
        queued = requeue_for_rescore(session, config.profile.sha256, top=top, since=since_utc)
    console.print(f"queued {queued} jobs for re-scoring under profile {config.profile.sha256[:12]}")
    _run_scoring(
        config,
        session_factory,
        mode=mode,
        command="rescore",
        postings_per_call=postings_per_call,
        max_calls=max_calls,
        max_input_tokens=max_input_tokens,
        dry_run=dry_run,
    )


@app.command()
def status(ctx: typer.Context) -> None:
    """Pending count, last run's stop reason, calls and spend today, token expiry."""
    config = _load(ctx)
    settings = config.settings
    with _db(config)() as session:
        st = scoring_status(
            session,
            config.profile.sha256,
            settings.schedule.timezone,
            settings.scoring.cli.token_created,
            utcnow(),
        )
    console.print(f"backend: {settings.scoring.backend}  model: {settings.scoring.model}")
    console.print(f"pending job groups: {st.pending}  batches in flight: {st.batches_in_flight}")
    if st.last_run is None:
        console.print("last run: none")
    else:
        run = st.last_run
        console.print(
            f"last run: #{run.id} {run.command} ({run.backend}, {run.mode}) "
            f"{run.started_at:%Y-%m-%d %H:%M} UTC, {run.calls_made} calls, "
            f"{run.postings_scored} scored, stop reason: {run.stop_reason or 'running'}"
        )
    console.print(
        f"today: {st.calls_today} calls, api spend ${st.spend_today_usd:.4f}, "
        f"cli API-equivalent ${st.cli_cost_today_usd:.4f} (not billed on Pro)"
    )
    if st.token_warning:
        console.print(f"[yellow]{escape(st.token_warning)}[/yellow]")


@app.command()
def calibrate(
    ctx: typer.Context,
    sample: Annotated[int, typer.Option("--sample", min=2, help="Job groups to score.")] = 50,
    sizes: Annotated[
        str, typer.Option("--sizes", help="CLI postings per call to compare.")
    ] = "20,40,60",
    seed: Annotated[int, typer.Option("--seed")] = 1,
    configs: Annotated[
        str | None,
        typer.Option(
            "--configs",
            help="Instead of --sizes: comma-separated backend:model:postings_per_call[:effort]; "
            "the first is the reference. E.g. api:claude-haiku-4-5:1,api:claude-haiku-5-5:1:low",
        ),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", help="Confirm: this spends API money and Pro prompts.")
    ] = False,
) -> None:
    """Compare batch sizes (api x1 reference vs cli at --sizes), or models with --configs."""
    config = _load(ctx)
    scoring = config.settings.scoring
    try:
        if configs:
            specs = [ConfigSpec.parse(x) for x in configs.split(",") if x.strip()]
        else:
            cli_sizes = [int(x) for x in sizes.split(",") if x.strip()]
            specs = [ConfigSpec("api", scoring.model, 1, scoring.effort)] + [
                ConfigSpec("cli", scoring.model, n, scoring.effort) for n in cli_sizes
            ]
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if len(specs) < 2:
        raise typer.BadParameter("need a reference and at least one candidate")
    calls = estimate_calls(sample, {s.name: s.size for s in specs})
    api_calls = sum(calls[s.name] for s in specs if s.backend == "api")
    cli_calls = sum(calls[s.name] for s in specs if s.backend == "cli")
    console.print(
        f"{sample} job groups: {api_calls} API requests (billed) and "
        f"{cli_calls} CLI prompts (Pro usage)"
    )
    if not yes:
        console.print("re-run with --yes to spend them")
        raise typer.Exit(1)
    try:
        built = [
            Configuration(
                s.name,
                make_scorer(
                    scoring.model_copy(update={"model": s.model, "effort": s.effort}),
                    s.backend,
                    os.environ,
                ),
                s.size,
            )
            for s in specs
        ]
    except RuntimeError as exc:
        err_console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(2) from exc
    reference, candidates = built[0], built[1:]
    with _db(config)() as session:
        ids = sample_job_ids(session, sample, seed)
        result = run_calibration(config, session, ids, reference, candidates)
    for configuration in (result.reference, *result.candidates):
        for n, call in enumerate(configuration.calls_detail, 1):
            if call["status"] != "ok" or call["valid"] < call["postings"]:
                console.print(
                    f"[yellow]{escape(configuration.name)} call {n}: {call['status']}, "
                    f"{call['valid']}/{call['postings']} valid, {call['rejected']} rejected: "
                    f"{escape(str(call['error'] or call['first_rejection'] or ''))[:300]}[/yellow]"
                )
    path = save(result, config.settings.paths.reports)
    for candidate in result.candidates:
        table = Table(title=f"{candidate.name} vs {result.reference.name}", title_justify="left")
        for column in ("title", "company", "ref", "score", "diff", "summary"):
            table.add_column(column, overflow="fold")
        for job_id in result.job_ids:
            ref, got = result.reference.scores.get(job_id), candidate.scores.get(job_id)
            diff = f"{got - ref:+d}" if ref is not None and got is not None else ""
            table.add_row(
                escape(result.titles[job_id][:50]),
                escape(result.companies[job_id][:25]),
                str(ref if ref is not None else "-"),
                str(got if got is not None else "-"),
                diff,
                escape(candidate.summaries.get(job_id, "")[:80]),
            )
        console.print(table)
        console.print(
            f"  {result.diff_stats(candidate)}  calls={candidate.calls} "
            f"failed={candidate.failed_calls} ${candidate.cost_usd:.4f} {candidate.seconds}s "
            f"tokens in={candidate.input_tokens:,} out={candidate.output_tokens:,}"
        )
    ref = result.reference
    console.print(
        f"reference: calls={ref.calls} failed={ref.failed_calls} ${ref.cost_usd:.4f} "
        f"{ref.seconds}s tokens in={ref.input_tokens:,} out={ref.output_tokens:,}"
    )
    console.print(f"saved to {path}")


@app.command("filter")
def filter_(
    ctx: typer.Context,
    rerun: Annotated[
        bool, typer.Option("--rerun", help="Re-evaluate every job, not just new or stale ones.")
    ] = False,
    samples: Annotated[
        int,
        typer.Option("--sample", min=0, help="Also list N random passed and N dropped jobs."),
    ] = 0,
    seed: Annotated[int | None, typer.Option("--seed", help="Seed for --sample.")] = None,
) -> None:
    """Pre-filter new jobs with filters.yaml and report pass rates per source."""
    config = _load(ctx)
    session_factory = _db(config)
    result = run_prefilter(config, session_factory, rerun=rerun)
    console.print(f"evaluated {result.evaluated} job groups: {result.passed} passed")

    with session_factory() as session:
        table = Table(title="Pre-filter by source", title_justify="left")
        table.add_column("source", no_wrap=True)
        for column in ("postings", "duplicates", "judged", "passed", "dropped", "pass rate"):
            table.add_column(column, justify="right")
        totals = [0, 0, 0, 0]
        for r in source_report(session):
            rate = f"{r.pass_rate:.0%}" if r.pass_rate is not None else ""
            table.add_row(
                r.source_id,
                *map(str, (r.jobs, r.duplicates, r.evaluated, r.passed, r.dropped)),
                rate,
            )
            for i, n in enumerate((r.jobs, r.duplicates, r.evaluated, r.passed)):
                totals[i] += n
        jobs, dups, judged, passed = totals
        table.add_row(
            "[bold]total[/bold]",
            *map(str, (jobs, dups, judged, passed, judged - passed)),
            f"{passed / judged:.0%}" if judged else "",
            end_section=True,
        )
        console.print(table)
        console.print("Top drop rules:")
        for rule, count in drop_reasons(session):
            console.print(f"  {count:6d}  {escape(rule)}")
        for passed_flag, label in ((True, "passed"), (False, "dropped")):
            if not samples:
                break
            console.print(f"\nSample of {label} jobs (inspect with `job-scout explain ID`):")
            for job, verdict in sample(session, passed=passed_flag, n=samples, seed=seed):
                console.print(
                    f"  {job.id:>6}  {escape(job.source_id)}: {escape(job.title[:70])}"
                    f"  [dim]{escape((verdict.reason or '')[:110])}[/dim]"
                )


@app.command()
def explain(ctx: typer.Context, job_id: int) -> None:
    """Show a job, its duplicate group, the pre-filter verdict and any scores."""
    config = _load(ctx)
    prefilter = build_prefilter(config)
    with _db(config)() as session:
        group = group_of(session, job_id)
        if group is None:
            err_console.print(f"[red]no job with id {job_id}[/red]")
            raise typer.Exit(1)
        job = next(j for j in group.jobs if j.id == job_id)
        canonical = group.canonical
        console.print(f"[bold]#{job.id} {escape(job.title)}[/bold]")
        fields = {
            "company": job.company,
            "source": job.source_id,
            "url": job.canonical_url,
            "apply": job.apply_url,
            "location": job.location_raw,
            "remote": job.remote_raw,
            "salary": _salary_text(job, prefilter.annual_salary(job)),
            "posted": job.posted_at.isoformat() if job.posted_at else None,
            "seen": f"{job.first_seen_at:%Y-%m-%d %H:%M} .. {job.last_seen_at:%Y-%m-%d %H:%M} UTC",
            "tags": ", ".join(job.tags) or None,
            "normalized": f"{job.company_normalized} | {job.title_normalized}",
        }
        for name, value in fields.items():
            if value:
                console.print(f"  {name:<10} {escape(str(value))}")
        if job.duplicate_of:
            console.print(f"  [yellow]duplicate of #{canonical.id}; judged with its group[/yellow]")

        if len(group.jobs) > 1:
            console.print(f"\n[bold]Duplicate group[/bold] ({len(group.jobs)} postings)")
            for member in group.jobs:
                marker = "*" if member.id == canonical.id else " "
                console.print(
                    f" {marker}#{member.id:<6} {escape(member.source_id)}: "
                    f"{escape(member.title[:60])}  [dim]{escape(member.location_raw or '-')}[/dim]"
                )
            console.print("  combined locations (sent to Claude when scoring):")
            for location in group.combined_locations():
                console.print(f"    - {escape(location)}")

        verdict = session.get(PrefilterResult, canonical.id)
        console.print("\n[bold]Pre-filter[/bold]")
        if verdict is None:
            console.print("  not evaluated yet (run `job-scout filter`)")
        else:
            state = "[green]passed[/green]" if verdict.passed else "[red]dropped[/red]"
            stale = (
                "" if verdict.fingerprint == prefilter.fingerprint else " [yellow](stale)[/yellow]"
            )
            console.print(f"  {state}{stale}  {escape(verdict.reason or '')}")
            console.print(f"  evaluated {verdict.evaluated_at:%Y-%m-%d %H:%M} UTC")

        console.print("\n[bold]Scores[/bold]")
        scores = session.scalars(select(Score).where(Score.job_id == canonical.id)).all()
        if not scores:
            console.print("  none yet")
        for score in scores:
            current = score.profile_hash == config.profile.sha256
            console.print(
                f"  {score.status} overall={score.overall_score} backend={score.backend} "
                f"model={score.model} profile={score.profile_hash[:12]}"
                f"{'' if current else ' [yellow](older profile)[/yellow]'}"
            )
            if score.result:
                console.print_json(json.dumps(score.result))

        vote = get_vote(session, canonical.id)
        if vote is not None:
            thumb = "up" if vote.vote == "up" else "down"
            reacted = f" to score {vote.overall_score}" if vote.overall_score is not None else ""
            console.print(
                f"\n[bold]Your feedback[/bold]\n  thumbs {thumb}{reacted} "
                f"({vote.updated_at:%Y-%m-%d}){': ' + escape(vote.reason) if vote.reason else ''}"
            )


def _salary_text(job: Job, annual: float | None) -> str | None:
    if not (job.salary_min or job.salary_max):
        return None
    amounts = " - ".join(f"{v:,.0f}" if v else "?" for v in (job.salary_min, job.salary_max))
    period = job.salary_period.value if job.salary_period else "?"
    text = f"{amounts} {job.salary_currency or '?'}/{period}"
    return f"{text} (~{annual:,.0f}/yr)" if annual is not None else text


def _age(when: datetime | None, now: datetime) -> str:
    if when is None:
        return "never"
    hours = (now - when).total_seconds() / 3600
    return f"{hours:.1f}h ago" if hours < 48 else f"{hours / 24:.0f}d ago"


def _health(source: SourceConfig, state: Source | None, last_run: SourceRun | None) -> str:
    if source.method in NOT_YET:
        return f"[dim]{NOT_YET[source.method]}[/dim]"
    if source.auth_env and not os.environ.get(source.auth_env):
        return f"[yellow]needs {source.auth_env}[/yellow]"
    if state is None or state.last_run_at is None:
        return "[dim]never run[/dim]"
    if state.consecutive_failures:
        error = escape((last_run.error or "")[:80]) if last_run else ""
        return f"[red]failing x{state.consecutive_failures}[/red] {error}"
    if state.consecutive_zero_runs >= 3:
        return f"[yellow]0 items x{state.consecutive_zero_runs}[/yellow]"
    return "[green]ok[/green]"


@sources_app.command("check")
def sources_check(
    ctx: typer.Context,
    tier: TierOpt = None,
    probe: Annotated[
        bool,
        typer.Option(
            "--probe", help="Also fetch every endpoint now (stores nothing; spends request budget)."
        ),
    ] = False,
) -> None:
    """Validate config and show each enabled source's health."""
    config = _load(ctx)
    settings = config.settings
    enabled = config.sources.enabled(tier)
    console.print(
        f"config: [bold]{config.config_dir}[/bold]  "
        f"profile: {config.profile.sha256[:12]}  "
        f"backend: {settings.scoring.backend}  model: {settings.scoring.model}"
    )

    session_factory = _db(config)
    now = utcnow()
    with session_factory() as session:
        states = {s.id: s for s in session.scalars(select(Source))}
        last_runs = {
            run.source_id: run
            for run in session.scalars(
                select(SourceRun)
                .where(SourceRun.kind == SourceRunKind.FETCH)
                .order_by(SourceRun.started_at)
            )
        }

    table = Table(title=f"Enabled sources ({len(enabled)})", title_justify="left")
    table.add_column("id", no_wrap=True)
    table.add_column("tier", justify="right")
    table.add_column("method")
    table.add_column("checked", no_wrap=True)
    table.add_column("last success", no_wrap=True)
    table.add_column("items", justify="right")
    table.add_column("new", justify="right")
    table.add_column("health")
    for source in enabled:
        state, last_run = states.get(source.id), last_runs.get(source.id)
        checked = str(source.checked)
        table.add_row(
            source.id,
            str(source.tier),
            source.method,
            f"[yellow]{checked}[/yellow]" if checked == "unverified" else checked,
            _age(state.last_success_at if state else None, now),
            str(state.last_item_count) if state and state.last_item_count is not None else "",
            str(last_run.items_new) if last_run and last_run.status == "ok" else "",
            _health(source, state, last_run),
        )
    console.print(table)

    disabled = [s.id for s in config.sources.sources if not s.is_enabled]
    console.print(
        f"disabled (opt in with `enabled: true`): {', '.join(disabled) or 'none'}\n"
        f"skipped: {len(config.sources.skipped)}  "
        f"watchlist: {', '.join(w.company for w in config.sources.watchlist) or 'none'}  "
        f"ATS platforms: {', '.join(config.sources.ats_platforms)}"
    )
    with session_factory() as session:
        boards = ats_counts(session)
    if boards:
        ok = {ats: n for (status, ats), n in boards.items() if status == "ok"}
        others = {status: 0 for status, _ in boards if status != "ok"}
        for (status, _), n in boards.items():
            if status != "ok":
                others[status] += n
        console.print(
            f"company boards: {sum(ok.values())} polled "
            f"({', '.join(f'{ats} {n}' for ats, n in sorted(ok.items()))})"
            + "".join(f", {n} {status}" for status, n in sorted(others.items()))
        )
    else:
        console.print("company boards: none yet (`job-scout ats detect`)")
    if probe:
        outcomes = _fetch(config, source_ids=[s.id for s in enabled], tier=None, probe=True)
        _print_outcomes(outcomes, "Probe results (nothing stored)")


def _print_refresh(report: RefreshReport) -> None:
    if report.seed_downloaded:
        c = report.changes
        console.print(
            f"seed: {report.seed_total} companies, {report.seed_kept} kept by the filters "
            f"({c.added} new, {c.updated} changed, {c.dropped} dropped)"
        )
    detected = ", ".join(f"{n} {ats}" for ats, n in report.detected.most_common()) or "none"
    console.print(
        f"watchlist: {report.watchlist} board(s); detected this run: {detected}; "
        f"{report.remaining} still to detect; {report.requests} requests"
    )


@ats_app.command("detect")
def ats_detect(
    ctx: typer.Context,
    refresh: Annotated[
        bool, typer.Option("--refresh", help="Re-detect every company, not just new or stale ones.")
    ] = False,
    limit: Annotated[
        int | None, typer.Option("--limit", min=1, help="Detect at most N companies this run.")
    ] = None,
    no_seed: Annotated[
        bool, typer.Option("--no-seed", help="Use the stored company list; don't download it.")
    ] = False,
) -> None:
    """Download the remoteintech seed, apply the watchlist, and find each company's job board.

    Reports how many companies mapped to each ATS and lists the unknown ones (also saved as
    reports/ats-unknown-<time>.csv). Safe to interrupt: every detection is saved as it goes.
    """
    config = _load(ctx)
    session_factory = _db(config)
    try:
        with HttpClient(config.sources.defaults) as http:
            report = refresh_ats(
                config,
                session_factory,
                http,
                download_seed=not no_seed,
                limit=limit,
                everything=refresh,
            )
    except FetchError as exc:
        err_console.print(f"[red]ats detect failed:[/red] {escape(str(exc))}")
        raise typer.Exit(1) from exc
    _print_refresh(report)

    with session_factory() as session:
        table = Table(title="Company boards", title_justify="left")
        for column in ("status", "ats", "companies"):
            table.add_column(column, justify="right" if column == "companies" else "left")
        for (status, ats), n in sorted(ats_counts(session).items()):
            table.add_row(status, ats, str(n))
        console.print(table)
        unknown = list(
            session.scalars(
                select(AtsCompany)
                .where(AtsCompany.status == AtsStatus.UNKNOWN_ATS)
                .order_by(AtsCompany.company)
            )
        )
    if not unknown:
        return
    path = config.settings.paths.reports / f"ats-unknown-{utcnow():%Y%m%dT%H%M%SZ}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["company", "careers_url", "website", "region", "note"])
        for row in unknown:
            writer.writerow(
                [row.company, row.careers_url, row.website, row.region, row.last_poll_error]
            )
    table = Table(title=f"Unknown ATS ({len(unknown)}): review by hand", title_justify="left")
    for column in ("company", "careers page"):
        table.add_column(column, overflow="fold")
    for row in unknown[:30]:
        table.add_row(escape(row.company), escape(row.careers_url or row.website or ""))
    console.print(table)
    more = f"{len(unknown) - 30} more; " if len(unknown) > 30 else ""
    console.print(f"{more}full list with notes: {path}")


@feedback_app.command("serve")
def feedback_serve(ctx: typer.Context) -> None:
    """Serve the page the digest's thumbs up/down links open (the compose `feedback` service).

    Listens on feedback.host:feedback.port; needs JOB_SCOUT_FEEDBACK_SECRET.
    """
    config = _load(ctx)
    settings = config.settings.feedback
    try:
        secret = read_secret(os.environ)
    except SecretError as exc:
        err_console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(2) from exc
    if not settings.base_url:
        err_console.print(
            "[yellow]feedback.base_url is not set: digests won't have feedback links[/yellow]"
        )
    server = make_server(FeedbackApp(config, _db(config), secret), settings.host, settings.port)
    console.print(f"feedback page listening on http://{settings.host}:{settings.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _judgment_rows(title: str, examples: list[Example]) -> Table:
    table = Table(title=title, title_justify="left")
    for column in ("job", "vote", "score", "posting", "your reason"):
        table.add_column(column, overflow="fold", justify="right" if column == "score" else "left")
    for e in examples:
        score = "-" if e.overall_score is None else str(e.overall_score)
        table.add_row(
            str(e.job_id),
            "👍" if e.vote == "up" else "👎",
            score + (" dq" if e.disqualified else ""),
            escape(f"{e.title} - {e.company or '?'}"),
            escape(e.reason or ""),
        )
    return table


@feedback_app.command("report")
def feedback_report(
    ctx: typer.Context,
    since: Annotated[
        datetime | None, typer.Option("--since", formats=["%Y-%m-%d"], help="Votes on/after.")
    ] = None,
    show_all: Annotated[
        bool, typer.Option("--all", help="List every judgment, not only disagreements.")
    ] = False,
) -> None:
    """How often Claude's scores agreed with your votes, and where they didn't."""
    config = _load(ctx)
    min_score = config.settings.digest.min_score
    since_utc = since.replace(tzinfo=ZoneInfo(config.settings.schedule.timezone)) if since else None
    with _db(config)() as session:
        stats = feedback_stats(session, min_score, since_utc)
    if not stats.judgments:
        console.print("no feedback yet")
        return

    def avg(examples: list[Example]) -> str:
        value = stats.mean_score(examples)
        return "-" if value is None else f"{value:.0f}"

    console.print(
        f"{len(stats.judgments)} judgments: {len(stats.up)} 👍 (mean score {avg(stats.up)}), "
        f"{len(stats.down)} 👎 (mean score {avg(stats.down)}); digest threshold {min_score}"
    )
    table = Table(title="Claude's score vs your vote", title_justify="left")
    table.add_column("")
    table.add_column(f"score >= {min_score}", justify="right")
    table.add_column(f"below {min_score} or disqualified", justify="right")
    table.add_row("👍", str(len(stats.up_agreed)), f"[yellow]{len(stats.up_missed)}[/yellow]")
    table.add_row("👎", f"[yellow]{len(stats.down_wrong)}[/yellow]", str(len(stats.down_agreed)))
    console.print(table)
    if stats.agreement is not None:
        console.print(f"agreement: {stats.agreement:.0%}")
    notes = []
    if stats.unscored:
        notes.append(f"{len(stats.unscored)} voted before a score existed")
    older = [
        e for e in stats.judgments if e.profile_hash and e.profile_hash != config.profile.sha256
    ]
    if older:
        notes.append(f"{len(older)} reacted to scores under an older profile.md")
    if notes:
        console.print(f"[dim]{'; '.join(notes)}[/dim]")
    if stats.disagreements:
        console.print(
            _judgment_rows(
                f"Disagreements ({len(stats.disagreements)}): patterns here may belong in "
                "profile.md",
                stats.disagreements,
            )
        )
    if show_all:
        console.print(_judgment_rows(f"All judgments ({len(stats.judgments)})", stats.judgments))
