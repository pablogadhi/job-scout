"""`score`, `rescore`, `status` and `calibrate`: everything that talks to Claude."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo

import typer
from rich.markup import escape
from rich.table import Table
from sqlalchemy.orm import Session, sessionmaker

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
from job_scout.db.models import Backend, ScoringMode, StopReason
from job_scout.db.types import utcnow
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
    config = load(ctx, scoring_overrides(backend, model))
    run_scoring(
        config,
        open_db(config),
        mode=mode,
        command="score",
        postings_per_call=postings_per_call,
        max_calls=max_calls,
        max_input_tokens=max_input_tokens,
        dry_run=dry_run,
    )


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
        raise fail("give --top N and/or --since YYYY-MM-DD")
    config = load(ctx, scoring_overrides(backend, model))
    session_factory = open_db(config)
    with session_factory.begin() as session:
        since_utc = (
            since.replace(tzinfo=ZoneInfo(config.settings.schedule.timezone)) if since else None
        )
        queued = requeue_for_rescore(session, config.profile.sha256, top=top, since=since_utc)
    console.print(f"queued {queued} jobs for re-scoring under profile {config.profile.sha256[:12]}")
    run_scoring(
        config,
        session_factory,
        mode=mode,
        command="rescore",
        postings_per_call=postings_per_call,
        max_calls=max_calls,
        max_input_tokens=max_input_tokens,
        dry_run=dry_run,
    )


def status(ctx: typer.Context) -> None:
    """Pending count, last run's stop reason, calls and spend today, token expiry."""
    config = load(ctx)
    settings = config.settings
    with open_db(config)() as session:
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
    config = load(ctx)
    scoring = config.settings.scoring
    try:
        if configs:
            specs = [ConfigSpec.parse(x) for x in configs.split(",") if x.strip()]
        else:
            cli_sizes = [int(x) for x in sizes.split(",") if x.strip()]
            specs = [ConfigSpec(Backend.API, scoring.model, 1, scoring.effort)] + [
                ConfigSpec(Backend.CLI, scoring.model, n, scoring.effort) for n in cli_sizes
            ]
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if len(specs) < 2:
        raise typer.BadParameter("need a reference and at least one candidate")
    calls = estimate_calls(sample, {s.name: s.size for s in specs})
    api_calls = sum(calls[s.name] for s in specs if s.backend == Backend.API)
    cli_calls = sum(calls[s.name] for s in specs if s.backend == Backend.CLI)
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
        raise fail(str(exc)) from exc
    reference, candidates = built[0], built[1:]
    with open_db(config)() as session:
        ids = sample_job_ids(session, sample, seed)
        result = run_calibration(config, session, ids, reference, candidates)
    for configuration in (result.reference, *result.candidates):
        for n, call in enumerate(configuration.calls_detail, 1):
            if call.status != "ok" or call.valid < call.postings:
                console.print(
                    f"[yellow]{escape(configuration.name)} call {n}: {call.status}, "
                    f"{call.valid}/{call.postings} valid, {call.rejected} rejected: "
                    f"{escape(str(call.error or call.first_rejection or ''))[:300]}[/yellow]"
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


def run_scoring(
    config: AppConfig,
    session_factory: sessionmaker[Session],
    *,
    mode: ScoringMode,
    command: str,
    postings_per_call: int | None = None,
    max_calls: int | None = None,
    max_input_tokens: int | None = None,
    dry_run: bool = False,
) -> RunReport:
    """One scoring run with the configured backend; prints its summary."""
    scoring = config.settings.scoring
    limits, use_batches = _limits(config, mode, postings_per_call, max_calls, max_input_tokens)
    try:
        # A dry run only plans, so it needs no credentials.
        scorer = (
            PlanOnlyScorer(scoring.backend, scoring.model)
            if dry_run
            else make_scorer(scoring, scoring.backend, os.environ)
        )
    except RuntimeError as exc:
        raise fail(str(exc)) from exc
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
    color = "green" if report.stop_reason == StopReason.DONE else "yellow"
    console.print(
        f"run #{report.run_id}: {report.calls_made} calls, {report.scored} scored, "
        f"{report.failed} failed, {report.pending_left} still pending, "
        f"est. ${report.cost_usd:.4f}"
        f"{' (API-equivalent, not billed on Pro)' if scoring.backend == Backend.CLI else ''}; "
        f"stop reason: [{color}]{stop}[/{color}]"
    )
    if report.batch_id:
        console.print(f"batch {report.batch_id}")
    return report


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
            mode,
            postings_per_call=postings_per_call,
            max_calls=max_calls,
            max_input_tokens_per_call=max_input_tokens,
        )
    except ValueError as exc:
        raise fail(str(exc)) from exc
    return Limits.from_settings(limits), bool(getattr(limits, "use_batches_api", False))


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
