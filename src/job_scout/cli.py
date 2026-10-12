"""job-scout command line (PLAN.md §9).

Every command loads and validates all of config/ first, so a typo fails before any work.
Flags override env vars, which override settings.yaml.
"""

from __future__ import annotations

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
from job_scout.db.models import Backend, ScoringMode, Source, SourceRun, SourceRunKind
from job_scout.db.types import utcnow
from job_scout.fetch.http import HttpClient, Recorder
from job_scout.fetch.runner import NOT_YET, SourceOutcome, run_fetch
from job_scout.log import setup_logging

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


@app.command()
def explain(ctx: typer.Context, job_id: int) -> None:
    """Show a job's pre-filter reason and full score JSON."""
    _load(ctx)
    _not_implemented(2)


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
