"""`feedback serve` and `feedback report`: your thumbs up/down."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo

import typer
from rich.markup import escape
from rich.table import Table

from job_scout.cli.common import console, err_console, fail, load, open_db
from job_scout.db.models import Vote
from job_scout.feedback.examples import Example
from job_scout.feedback.links import SecretError, read_secret
from job_scout.feedback.report import feedback_stats
from job_scout.feedback.server import FeedbackApp, make_server


def serve(ctx: typer.Context) -> None:
    """Serve the page the digest's thumbs up/down links open (the compose `feedback` service).

    Listens on feedback.host:feedback.port; needs JOB_SCOUT_FEEDBACK_SECRET.
    """
    config = load(ctx)
    settings = config.settings.feedback
    try:
        secret = read_secret(os.environ)
    except SecretError as exc:
        raise fail(str(exc)) from exc
    if not settings.base_url:
        err_console.print(
            "[yellow]feedback.base_url is not set: digests won't have feedback links[/yellow]"
        )
    server = make_server(FeedbackApp(config, open_db(config), secret), settings.host, settings.port)
    console.print(f"feedback page listening on http://{settings.host}:{settings.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def report(
    ctx: typer.Context,
    since: Annotated[
        datetime | None, typer.Option("--since", formats=["%Y-%m-%d"], help="Votes on/after.")
    ] = None,
    show_all: Annotated[
        bool, typer.Option("--all", help="List every judgment, not only disagreements.")
    ] = False,
) -> None:
    """How often Claude's scores agreed with your votes, and where they didn't."""
    config = load(ctx)
    min_score = config.settings.digest.min_score
    since_utc = since.replace(tzinfo=ZoneInfo(config.settings.schedule.timezone)) if since else None
    with open_db(config)() as session:
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


def _judgment_rows(title: str, examples: list[Example]) -> Table:
    table = Table(title=title, title_justify="left")
    for column in ("job", "vote", "score", "posting", "your reason"):
        table.add_column(column, overflow="fold", justify="right" if column == "score" else "left")
    for e in examples:
        score = "-" if e.overall_score is None else str(e.overall_score)
        table.add_row(
            str(e.job_id),
            "👍" if e.vote == Vote.UP else "👎",
            score + (" dq" if e.disqualified else ""),
            escape(f"{e.title} - {e.company or '?'}"),
            escape(e.reason or ""),
        )
    return table
