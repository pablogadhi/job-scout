"""job-scout command line (PLAN.md §9).

Every command loads and validates all of config/ first, so a typo fails before any work.
Flags override env vars, which override settings.yaml.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from job_scout.config import DEFAULT_CONFIG_DIR, AppConfig, ConfigError, SourceConfig, load_config
from job_scout.db import SchemaOutdated, check_schema, make_engine, make_session_factory
from job_scout.db.models import (
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
from job_scout.fetch.http import HttpClient, Recorder
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

log = logging.getLogger(__name__)
console = Console()
err_console = Console(stderr=True)

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)
sources_app = typer.Typer(no_args_is_help=True, help="Inspect job sources.")
ats_app = typer.Typer(no_args_is_help=True, help="ATS company seed (remoteintech + watchlist).")
app.add_typer(sources_app, name="sources")
app.add_typer(ats_app, name="ats")


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


def _not_implemented(phase: int) -> NoReturn:
    err_console.print(f"[yellow]not implemented yet (PLAN.md Phase {phase})[/yellow]")
    raise typer.Exit(1)


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
    """Score pending postings with Claude."""
    _load(ctx, _scoring_overrides(backend, model))
    _not_implemented(3)


@app.command()
def digest(ctx: typer.Context, dry_run: DryRunOpt = False) -> None:
    """Render and send the digest of newly scored jobs."""
    _load(ctx)
    _not_implemented(4)


@app.command()
def run(ctx: typer.Context) -> None:
    """Daily pipeline: fetch, filter, score (daily mode), digest."""
    _load(ctx)
    _not_implemented(4)


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
    """Fetch everything, score in backlog mode, write a ranked report."""
    _load(ctx, _scoring_overrides(backend, model))
    _not_implemented(4)


@app.command()
def rescore(
    ctx: typer.Context,
    top: Annotated[int | None, typer.Option("--top", min=1)] = None,
    since: Annotated[str | None, typer.Option("--since", help="YYYY-MM-DD")] = None,
    backend: BackendOpt = None,
    model: ModelOpt = None,
    postings_per_call: PostingsPerCallOpt = None,
    max_calls: MaxCallsOpt = None,
    max_input_tokens: MaxInputTokensOpt = None,
) -> None:
    """Re-score recent or top jobs under the current profile."""
    _load(ctx, _scoring_overrides(backend, model))
    _not_implemented(3)


@app.command()
def status(ctx: typer.Context) -> None:
    """Pending count, last run's stop reason, calls and spend today, token expiry."""
    _load(ctx)
    _not_implemented(3)


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
    if probe:
        outcomes = _fetch(config, source_ids=[s.id for s in enabled], tier=None, probe=True)
        _print_outcomes(outcomes, "Probe results (nothing stored)")


@ats_app.command("detect")
def ats_detect(ctx: typer.Context) -> None:
    """(Re)build ats_companies from remoteintech and the watchlist."""
    _load(ctx)
    _not_implemented(5)
