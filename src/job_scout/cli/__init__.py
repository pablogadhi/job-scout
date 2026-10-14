"""job-scout command line (PLAN.md §9).

Every command loads and validates all of config/ first, so a typo fails before any work.
Flags override env vars, which override settings.yaml.
"""

# The commands live in one module per area; this file only registers them, in --help order.

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from job_scout.cli import ats, digest, feedback, jobs, pipeline, scoring, sources
from job_scout.cli.common import State
from job_scout.config import DEFAULT_CONFIG_DIR

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)
sources_app = typer.Typer(no_args_is_help=True, help="Inspect job sources.")
ats_app = typer.Typer(no_args_is_help=True, help="ATS company seed (remoteintech + watchlist).")
feedback_app = typer.Typer(no_args_is_help=True, help="Thumbs up/down from the digest.")
app.add_typer(sources_app, name="sources")
app.add_typer(ats_app, name="ats")
app.add_typer(feedback_app, name="feedback")


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


app.command()(sources.fetch)
app.command()(scoring.score)
app.command()(digest.digest)
app.command()(pipeline.run)
app.command()(pipeline.backlog)
app.command("notify-failure")(digest.notify_failure)
app.command()(jobs.export)
app.command()(scoring.rescore)
app.command()(scoring.status)
app.command()(scoring.calibrate)
app.command("filter")(jobs.filter_)
app.command()(jobs.explain)
sources_app.command("check")(sources.check)
ats_app.command("detect")(ats.detect)
feedback_app.command("serve")(feedback.serve)
feedback_app.command("report")(feedback.report)
