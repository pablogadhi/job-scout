"""`filter`, `explain` and `export`: the stored jobs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table
from sqlalchemy import select

from job_scout.cli.common import console, fail, load, open_db
from job_scout.db.groups import group_of
from job_scout.db.models import Job, PrefilterResult, Score
from job_scout.db.types import utcnow
from job_scout.feedback.store import get_vote
from job_scout.prefilter.runner import (
    build_prefilter,
    drop_reasons,
    run_prefilter,
    sample,
    source_report,
)
from job_scout.report.data import extra_output_fields
from job_scout.report.export import export_csv, snapshot_sqlite


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
    config = load(ctx)
    session_factory = open_db(config)
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


def explain(ctx: typer.Context, job_id: int) -> None:
    """Show a job, its duplicate group, the pre-filter verdict and any scores."""
    config = load(ctx)
    prefilter = build_prefilter(config)
    with open_db(config)() as session:
        group = group_of(session, job_id)
        if group is None:
            raise fail(f"no job with id {job_id}", 1)
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
            reacted = f" to score {vote.overall_score}" if vote.overall_score is not None else ""
            console.print(
                f"\n[bold]Your feedback[/bold]\n  thumbs {vote.vote}{reacted} "
                f"({vote.updated_at:%Y-%m-%d}){': ' + escape(vote.reason) if vote.reason else ''}"
            )


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
    config = load(ctx)
    directory = out or config.settings.paths.exports
    stamp = f"{utcnow():%Y%m%dT%H%M%SZ}"
    with open_db(config)() as session:
        path = directory / f"jobs-{stamp}.csv"
        rows = export_csv(session, path, extra_output_fields(config.profile))
    console.print(f"{rows} postings -> {path}")
    if sqlite:
        try:
            snapshot = snapshot_sqlite(config.settings.db_url, directory / f"job-scout-{stamp}.db")
        except ValueError as exc:
            raise fail(str(exc)) from exc
        console.print(f"database snapshot -> {snapshot}")


def _salary_text(job: Job, annual: float | None) -> str | None:
    if not (job.salary_min or job.salary_max):
        return None
    amounts = " - ".join(f"{v:,.0f}" if v else "?" for v in (job.salary_min, job.salary_max))
    period = job.salary_period.value if job.salary_period else "?"
    text = f"{amounts} {job.salary_currency or '?'}/{period}"
    return f"{text} (~{annual:,.0f}/yr)" if annual is not None else text
