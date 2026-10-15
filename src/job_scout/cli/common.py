"""What every command shares: consoles, config and database loading, common options."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Annotated, Any

import typer
from rich.console import Console
from rich.markup import escape
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from job_scout.config import DEFAULT_CONFIG_DIR, AppConfig, ConfigError, load_config
from job_scout.db import SchemaOutdated, check_schema, make_engine, make_session_factory
from job_scout.db.models import Backend
from job_scout.log import setup_logging

log = logging.getLogger(__name__)

# Without a terminal Rich assumes 80 columns and squeezes tables into them, cutting headers
# and values to "…". Wide enough for every table at its natural width.
PIPE_WIDTH = 200


def make_console(*, stderr: bool = False, file: IO[str] | None = None) -> Console:
    """A console that prints whole tables when the output isn't a terminal (a pipe,
    `docker compose run -T`, the journal). A positive COLUMNS still decides the width; Rich
    would print nothing at all with COLUMNS=0."""
    out = Console(stderr=stderr, file=file)
    columns = os.environ.get("COLUMNS", "")
    if not out.is_terminal and not (columns.isdigit() and int(columns) > 0):
        out.width = PIPE_WIDTH
    return out


console = make_console()
err_console = make_console(stderr=True)


@dataclass
class State:
    config_dir: Path = DEFAULT_CONFIG_DIR
    overrides: dict[str, Any] = field(default_factory=dict)


def fail(message: str, code: int = 2, *, label: str | None = None) -> typer.Exit:
    """Print an error to stderr and return the exit to raise: `raise fail(str(exc)) from exc`.

    With `label`, only the label is red ("label: message"); otherwise the whole message is.
    """
    if label:
        err_console.print(f"[red]{label}:[/red] {escape(message)}", soft_wrap=True)
    else:
        err_console.print(f"[red]{escape(message)}[/red]")
    return typer.Exit(code)


def load(ctx: typer.Context, overrides: dict[str, Any] | None = None) -> AppConfig:
    state: State = ctx.obj
    merged = {**state.overrides, **(overrides or {})}
    try:
        config = load_config(state.config_dir, merged)
    except ConfigError as exc:
        raise fail(str(exc), label="config error") from exc
    setup_logging(config.settings.log_level)
    log.debug("config loaded from %s (profile %s)", state.config_dir, config.profile.sha256[:12])
    return config


def open_db(config: AppConfig) -> sessionmaker[Session]:
    engine = make_engine(config.settings.db_url)
    try:
        check_schema(engine)
    except (SchemaOutdated, OperationalError) as exc:
        raise fail(str(exc), label="database error") from exc
    return make_session_factory(engine)


def scoring_overrides(backend: Backend | None, model: str | None) -> dict[str, Any]:
    scoring: dict[str, Any] = {}
    if backend is not None:
        scoring["backend"] = backend
    if model is not None:
        scoring["model"] = model
    return {"scoring": scoring} if scoring else {}


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
