"""job-scout command line (PLAN.md §9).

Every command loads and validates all of config/ first, so a typo fails before any work.
Flags override env vars, which override settings.yaml.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from job_scout.config import DEFAULT_CONFIG_DIR, AppConfig, ConfigError, load_config
from job_scout.db.models import Backend, ScoringMode
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


@app.command()
def fetch(
    ctx: typer.Context,
    source: Annotated[str | None, typer.Option("--source", help="Only this source id.")] = None,
    tier: Annotated[int | None, typer.Option("--tier", min=1, max=3)] = None,
) -> None:
    """Fetch postings from enabled sources."""
    _load(ctx)
    _not_implemented(1)


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


@sources_app.command("check")
def sources_check(
    ctx: typer.Context,
    tier: Annotated[int | None, typer.Option("--tier", min=1, max=3)] = None,
) -> None:
    """Validate config and list enabled sources. (Endpoint checks arrive in Phase 1.)"""
    config = _load(ctx)
    settings = config.settings
    enabled = config.sources.enabled(tier)

    console.print(
        f"config: [bold]{config.config_dir}[/bold]  "
        f"profile: {config.profile.sha256[:12]}  "
        f"backend: {settings.scoring.backend}  model: {settings.scoring.model}"
    )
    table = Table(title=f"Enabled sources ({len(enabled)})", title_justify="left")
    table.add_column("id", no_wrap=True)
    table.add_column("name")
    table.add_column("tier", justify="right")
    table.add_column("method")
    table.add_column("checked", no_wrap=True)
    table.add_column("endpoints", justify="right")
    for source in enabled:
        checked = str(source.checked)
        table.add_row(
            source.id,
            source.name,
            str(source.tier),
            source.method,
            f"[yellow]{checked}[/yellow]" if checked == "unverified" else checked,
            str(len(source.endpoints)),
        )
    console.print(table)

    disabled = [s.id for s in config.sources.sources if not s.is_enabled]
    console.print(
        f"disabled (opt in with `enabled: true`): {', '.join(disabled) or 'none'}\n"
        f"skipped: {len(config.sources.skipped)}  "
        f"watchlist: {', '.join(w.company for w in config.sources.watchlist) or 'none'}  "
        f"ATS platforms: {', '.join(config.sources.ats_platforms)}"
    )


@ats_app.command("detect")
def ats_detect(ctx: typer.Context) -> None:
    """(Re)build ats_companies from remoteintech and the watchlist."""
    _load(ctx)
    _not_implemented(5)
