"""`fetch` and `sources check`."""

from __future__ import annotations

import dataclasses
import os
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table
from sqlalchemy import select

from job_scout.ats.store import counts as ats_counts
from job_scout.cli.common import SourceOpt, TierOpt, console, fail, load, open_db
from job_scout.config import AppConfig, SourceConfig
from job_scout.config.secrets import secret
from job_scout.db.models import AtsStatus, Source, SourceRun, SourceRunKind, SourceRunStatus
from job_scout.db.types import utcnow
from job_scout.fetch.http import HttpClient, Recorder
from job_scout.fetch.runner import NOT_YET, SourceOutcome, run_fetch


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
    max_pages: Annotated[
        int | None,
        typer.Option(
            "--max-pages",
            min=1,
            help="Sources with max_pages (findwork): read up to N pages this run.",
        ),
    ] = None,
) -> None:
    """Fetch postings from enabled sources and store new ones."""
    config = load(ctx)
    tests_dir = (Path.cwd() / "tests").resolve()
    if record and record.resolve().is_relative_to(tests_dir):
        raise fail(
            "fixtures are synthetic (tests/fixtures/make_http_fixtures.py). "
            "Record to data/recordings instead.",
            label="--record must not write under tests/",
        )
    if max_pages:
        config = with_max_pages(config, max_pages)
    outcomes = fetch_sources(config, source_ids=source or [], tier=tier, force=force, record=record)
    print_outcomes(outcomes, "Fetch results")
    exit_if_all_failed(outcomes)


def check(
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
    config = load(ctx)
    settings = config.settings
    enabled = config.sources.enabled(tier)
    console.print(
        f"config: [bold]{config.config_dir}[/bold]  "
        f"profile: {config.profile.sha256[:12]}  "
        f"backend: {settings.scoring.backend}  model: {settings.scoring.model}"
    )

    session_factory = open_db(config)
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
            str(last_run.items_new) if last_run and last_run.status == SourceRunStatus.OK else "",
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
        ok = {ats: n for (status, ats), n in boards.items() if status == AtsStatus.OK}
        others = {status: 0 for status, _ in boards if status != AtsStatus.OK}
        for (status, _), n in boards.items():
            if status != AtsStatus.OK:
                others[status] += n
        console.print(
            f"company boards: {sum(ok.values())} polled "
            f"({', '.join(f'{ats} {n}' for ats, n in sorted(ok.items()))})"
            + "".join(f", {n} {status}" for status, n in sorted(others.items()))
        )
    else:
        console.print("company boards: none yet (`job-scout ats detect`)")
    if probe:
        outcomes = fetch_sources(config, source_ids=[s.id for s in enabled], tier=None, probe=True)
        print_outcomes(outcomes, "Probe results (nothing stored)")


def with_max_pages(config: AppConfig, pages: int) -> AppConfig:
    """The config with every source that has max_pages reading `pages` instead."""
    sources = [
        s.model_copy(update={"max_pages": pages}) if s.max_pages else s
        for s in config.sources.sources
    ]
    return dataclasses.replace(
        config, sources=config.sources.model_copy(update={"sources": sources})
    )


def fetch_sources(
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
        raise fail(", ".join(unknown), label="unknown source id")
    session_factory = open_db(config)
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
            environ=os.environ,
        )


def print_outcomes(outcomes: list[SourceOutcome], title: str) -> None:
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


def exit_if_all_failed(outcomes: list[SourceOutcome]) -> None:
    """Exit 1 when every source that was tried failed (skipped ones don't count)."""
    attempted = [o for o in outcomes if o.status != "skipped"]
    if attempted and all(o.status == "failed" for o in attempted):
        raise typer.Exit(1)


def _age(when: datetime | None, now: datetime) -> str:
    if when is None:
        return "never"
    hours = (now - when).total_seconds() / 3600
    return f"{hours:.1f}h ago" if hours < 48 else f"{hours / 24:.0f}d ago"


def _health(source: SourceConfig, state: Source | None, last_run: SourceRun | None) -> str:
    if source.method in NOT_YET:
        return f"[dim]{NOT_YET[source.method]}[/dim]"
    if source.auth_env and secret(os.environ, source.auth_env) is None:
        return f"[yellow]needs {source.auth_env}[/yellow]"
    if state is None or state.last_run_at is None:
        return "[dim]never run[/dim]"
    if state.consecutive_failures:
        error = escape((last_run.error or "")[:80]) if last_run else ""
        return f"[red]failing x{state.consecutive_failures}[/red] {error}"
    if state.consecutive_zero_runs >= 3:
        return f"[yellow]0 items x{state.consecutive_zero_runs}[/yellow]"
    return "[green]ok[/green]"
