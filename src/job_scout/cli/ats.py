"""`ats detect`, and the weekly company-board upkeep that `run` and `backlog` do."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table
from sqlalchemy import select

from job_scout.ats.refresh import RefreshReport
from job_scout.ats.refresh import refresh as refresh_ats
from job_scout.ats.store import counts as ats_counts
from job_scout.cli.common import console, fail, load, open_db
from job_scout.config import AppConfig
from job_scout.db.models import AtsCompany, AtsStatus
from job_scout.db.types import utcnow
from job_scout.fetch.http import FetchError, HttpClient


def detect(
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
    """Download the remoteintech seed (when that source is on), apply the watchlist and
    sources.yaml `companies`, and find each company's job board.

    Reports how many companies mapped to each ATS and lists the unknown ones (also saved as
    reports/ats-unknown-<time>.csv). Safe to interrupt: every detection is saved as it goes.
    """
    config = load(ctx)
    session_factory = open_db(config)
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
        raise fail(str(exc), 1, label="ats detect failed") from exc
    print_refresh(report)

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
    _write_unknown_csv(path, unknown)
    table = Table(title=f"Unknown ATS ({len(unknown)}): review by hand", title_justify="left")
    for column in ("company", "careers page"):
        table.add_column(column, overflow="fold")
    for row in unknown[:30]:
        table.add_row(escape(row.company), escape(row.careers_url or row.website or ""))
    console.print(table)
    more = f"{len(unknown) - 30} more; " if len(unknown) > 30 else ""
    console.print(f"{more}full list with notes: {path}")


def upkeep(config: AppConfig) -> None:
    """Weekly seed download and capped board detection (settings.ats), for `run`/`backlog`."""
    if config.sources.board_source is None:
        return
    with HttpClient(config.sources.defaults) as http:
        report = refresh_ats(
            config, open_db(config), http, limit=config.settings.ats.max_detections_per_run
        )
    print_refresh(report)


def print_refresh(report: RefreshReport) -> None:
    if report.seed_downloaded:
        c = report.changes
        console.print(
            f"seed: {report.seed_total} companies, {report.seed_kept} kept by the filters "
            f"({c.added} new, {c.updated} changed, {c.dropped} dropped)"
        )
    detected = ", ".join(f"{n} {ats}" for ats, n in report.detected.most_common()) or "none"
    console.print(
        f"watchlist: {report.watchlist} board(s); companies: {report.companies}; "
        f"detected this run: {detected}; "
        f"{report.remaining} still to detect; {report.requests} requests"
    )


def _write_unknown_csv(path: Path, unknown: list[AtsCompany]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["company", "careers_url", "website", "region", "note"])
        for row in unknown:
            writer.writerow(
                [row.company, row.careers_url, row.website, row.region, row.last_poll_error]
            )
