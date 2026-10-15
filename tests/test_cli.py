import io
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from rich.table import Table
from sqlalchemy import select
from typer.testing import CliRunner

from job_scout.ats.refresh import RefreshReport
from job_scout.cli import app
from job_scout.cli.common import PIPE_WIDTH, make_console
from job_scout.db import make_engine, make_session_factory
from job_scout.db.models import Backend, Job, Source
from job_scout.fetch.models import Posting
from job_scout.fetch.store import store_postings
from job_scout.scoring.calibrate import ConfigSpec
from tests.conftest import REPO_CONFIG, edit
from tests.scoring_helpers import result_for, seed_jobs

runner = CliRunner()


def invoke(*args: str, config_dir: Path = REPO_CONFIG):
    return runner.invoke(app, ["--config-dir", str(config_dir), *args], env={"COLUMNS": "200"})


@pytest.mark.usefixtures("db_url")
def test_sources_check_lists_enabled_sources() -> None:
    result = invoke("sources", "check")
    assert result.exit_code == 0, result.output
    assert "Enabled sources (14)" in result.output
    assert "never run" in result.output
    assert "needs FINDWORK_API_KEY" in result.output
    assert "Canonical" in result.output


@pytest.mark.usefixtures("db_url")
def test_sources_check_by_tier() -> None:
    result = invoke("sources", "check", "--tier", "1")
    assert result.exit_code == 0, result.output
    table, disabled = result.output.split("disabled")
    assert "remoteok" in table
    assert "nodesk" not in table
    assert "nodesk" in disabled


def test_console_without_a_terminal_is_wide(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COLUMNS", raising=False)
    out = io.StringIO()
    console = make_console(file=out)
    assert console.width == PIPE_WIDTH
    table = Table()
    for name in ("id", "tier", "method", "checked", "last success", "items", "new", "health"):
        table.add_column(name)
    table.add_row("realworkfromanywhere", "1", "api", "2026-10-09", "46.0h ago", "312", "19",
                  "needs FINDWORK_API_KEY")  # fmt: skip
    console.print(table)
    assert "…" not in out.getvalue()
    assert "needs FINDWORK_API_KEY" in out.getvalue()
    monkeypatch.setenv("COLUMNS", "60")
    assert make_console(file=io.StringIO()).width == 60
    monkeypatch.setenv("COLUMNS", "0")
    assert make_console(file=io.StringIO()).width == PIPE_WIDTH


def test_bad_config_exits_2(config_dir: Path) -> None:
    edit(config_dir / "settings.yaml", "backend: cli", "backend: gpt")
    result = invoke("sources", "check", config_dir=config_dir)
    assert result.exit_code == 2
    assert "scoring.backend" in result.output


def test_unmigrated_database_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOB_SCOUT_DATABASE_URL", f"sqlite:///{tmp_path / 'empty.db'}")
    result = invoke("sources", "check")
    assert result.exit_code == 2
    assert "alembic upgrade head" in result.output


@pytest.mark.usefixtures("db_url")
def test_fetch_unknown_source_exits_2() -> None:
    result = invoke("fetch", "--source", "nope")
    assert result.exit_code == 2
    assert "unknown source id: nope" in result.output


def _seed_group(db_url: str) -> int:
    """A canonical EMEA-only posting plus a later LATAM copy; returns the copy's id."""
    engine = make_engine(db_url)
    sf = make_session_factory(engine)
    now = datetime(2026, 10, 12, tzinfo=UTC)
    with sf.begin() as session:
        session.add_all([Source(id="a"), Source(id="b")])
        session.flush()
        for source, job_id, location, when in (
            ("a", "1", "EMEA only", now),
            ("b", "2", "Latin America", now + timedelta(hours=1)),
        ):
            posting = Posting(
                source_job_id=job_id,
                canonical_url=f"https://board.example/{job_id}",
                title="Senior Platform Engineer",
                company="Northwind Cloud",
                location_raw=location,
                raw={},
            )
            store_postings(
                session, source, [posting], ["company_normalized", "title_normalized"], when
            )
    with sf() as session:
        copy_id = session.scalars(select(Job.id).where(Job.source_job_id == "2")).one()
    engine.dispose()
    return copy_id


def test_filter_and_explain(db_url: str) -> None:
    copy_id = _seed_group(db_url)
    result = invoke("explain", str(copy_id))
    assert result.exit_code == 0, result.output
    assert "not evaluated yet" in result.output

    result = invoke("filter", "--sample", "1", "--seed", "1")
    assert result.exit_code == 0, result.output
    assert "evaluated 1 job groups: 1 passed" in result.output
    assert "Pre-filter by source" in result.output
    assert "Sample of passed jobs" in result.output

    result = invoke("explain", str(copy_id))
    assert result.exit_code == 0, result.output
    assert f"duplicate of #{copy_id - 1}" in result.output
    assert "Duplicate group (2 postings)" in result.output
    assert "a: EMEA only" in result.output and "b: Latin America" in result.output
    assert f"via copy #{copy_id} on b" in result.output
    assert "none yet" in result.output  # no scores before Phase 3


@pytest.mark.usefixtures("db_url")
def test_explain_unknown_job_exits_1() -> None:
    result = invoke("explain", "999")
    assert result.exit_code == 1
    assert "no job with id 999" in result.output


def test_record_refuses_tests_dir() -> None:
    result = invoke("fetch", "--record", "tests/fixtures/http")
    assert result.exit_code == 2
    assert "fixtures are synthetic" in result.output


FAKE_CLAUDE = r'''#!{python}
"""Stand-in for the claude CLI: scores every posting on stdin; never calls a model."""
import json, os, re, sys

args = sys.argv[1:]
assert args[0] == "-p" and "--bare" not in args, args
assert "ANTHROPIC_API_KEY" not in os.environ, "API key leaked to the CLI"
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as log:
    log.write(json.dumps({{"args": args, "cwd": os.getcwd()}}) + "\n")
ids = [int(i) for i in re.findall(r'<posting job_id="(\d+)">', sys.stdin.read())]
result = dict(json.loads(os.environ["FAKE_RESULT"]))
results = [dict(result, job_id=i) for i in ids]
print(json.dumps({{"type": "result", "is_error": False, "result": "",
                  "structured_output": {{"results": results}}, "total_cost_usd": 0.01,
                  "usage": {{"input_tokens": 100, "output_tokens": 50}}}}))
'''


def _fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    script = tmp_path / "claude"
    script.write_text(FAKE_CLAUDE.format(python=sys.executable))
    script.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("JOB_SCOUT_SCORING__CLI__BINARY", str(script))
    monkeypatch.setenv("JOB_SCOUT_SCORING__CLI__WORKDIR", str(tmp_path / "cwd"))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("FAKE_RESULT", json.dumps(result_for(0, 81)))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-must-not-leak")
    return log


def test_score_with_cli_backend_end_to_end(
    db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = _fake_claude(tmp_path, monkeypatch)
    # The fake CLI needs the allowlisted env to carry its own settings through.
    monkeypatch.setattr(
        "job_scout.scoring.backends.cli.ENV_ALLOWLIST", ("PATH", "FAKE_CLAUDE_LOG", "FAKE_RESULT")
    )
    engine = make_engine(db_url)
    ids = seed_jobs(make_session_factory(engine), 25)
    engine.dispose()

    result = invoke("score", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "dry run: 2 calls planned (1x20 + 1x5 postings, 25 total)" in result.output
    assert not log.exists()

    result = invoke("score", "--max-calls", "1")
    assert result.exit_code == 0, result.output
    assert "1 calls, 20 scored" in result.output and "stop reason: max_calls" in result.output
    call = json.loads(log.read_text().splitlines()[0])
    assert call["cwd"] == str(tmp_path / "cwd")
    assert call["args"][call["args"].index("--model") + 1] == "claude-haiku-5-5"

    result = invoke("score")
    assert result.exit_code == 0, result.output
    assert "5 scored" in result.output and "stop reason: done" in result.output

    result = invoke("explain", str(ids[0]))
    assert '"overall_score": 81' in result.output and "backend=cli" in result.output

    result = invoke("status")
    assert result.exit_code == 0, result.output
    assert "pending job groups: 0" in result.output
    assert "stop reason: done" in result.output
    assert "token_created" in result.output  # creation date not configured: warns


@pytest.mark.usefixtures("db_url")
def test_score_api_backend_needs_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = invoke("score", "--backend", "api")
    assert result.exit_code == 2
    assert "ANTHROPIC_API_KEY is not set" in result.output


@pytest.mark.usefixtures("db_url")
def test_api_rejects_cli_only_limits() -> None:
    result = invoke("score", "--backend", "api", "--max-input-tokens", "1000")
    assert result.exit_code == 2
    assert "max_input_tokens_per_call not valid for the api backend" in result.output


@pytest.mark.usefixtures("db_url")
def test_calibrate_requires_confirmation() -> None:
    result = invoke("calibrate", "--sample", "50")
    assert result.exit_code == 1
    assert "50 API requests (billed) and 6 CLI prompts" in result.output
    assert "--yes" in result.output


@pytest.mark.usefixtures("db_url")
def test_calibrate_compares_models() -> None:
    result = invoke(
        "calibrate",
        "--sample",
        "50",
        "--configs",
        "api:claude-haiku-4-5:1,api:claude-haiku-5-5:1:low,cli:claude-haiku-5-5:20",
    )
    assert result.exit_code == 1
    assert "100 API requests (billed) and 3 CLI prompts" in result.output
    bad = invoke("calibrate", "--configs", "api:claude-haiku-4-5")
    assert bad.exit_code == 2 and "backend:model:postings_per_call" in bad.output


def test_config_spec() -> None:
    spec = ConfigSpec.parse("cli:claude-haiku-5-5:20:low")
    assert spec == ConfigSpec(Backend.CLI, "claude-haiku-5-5", 20, "low")
    assert spec.name == "cli claude-haiku-5-5 x20 effort=low"
    assert ConfigSpec.parse("api:claude-haiku-4-5:1").effort is None
    with pytest.raises(ValueError, match="effort must be one of low, medium"):
        ConfigSpec.parse("api:claude-haiku-5-5:1:extreme")


@pytest.mark.usefixtures("db_url")
def test_dry_run_needs_no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = invoke("score", "--backend", "api", "--mode", "backlog", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "Batches API" in result.output and "dry run: 0 calls planned" in result.output


def _pipeline_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fake claude, reports under tmp_path, and no network: fetching returns nothing."""
    _fake_claude(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "job_scout.scoring.backends.cli.ENV_ALLOWLIST", ("PATH", "FAKE_CLAUDE_LOG", "FAKE_RESULT")
    )
    monkeypatch.setattr("job_scout.cli.sources.fetch_sources", lambda *a, **k: [])
    monkeypatch.setattr("job_scout.cli.ats.refresh_ats", lambda *a, **k: RefreshReport())
    reports = tmp_path / "reports"
    monkeypatch.setenv("JOB_SCOUT_PATHS__REPORTS", str(reports))
    monkeypatch.setenv("JOB_SCOUT_DIGEST__EMAIL__ENABLED", "false")  # whatever settings.yaml says
    return reports


def _seed(db_url: str, count: int, start: int = 1) -> list[int]:
    engine = make_engine(db_url)
    ids = seed_jobs(make_session_factory(engine), count, start=start)
    engine.dispose()
    return ids


def test_run_daily_pipeline_digests_only_new_jobs(
    db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reports = _pipeline_env(tmp_path, monkeypatch)
    _seed(db_url, 3)
    result = invoke("run")
    assert result.exit_code == 0, result.output
    assert "pre-filter: evaluated 3 job groups, 3 passed" in result.output
    assert "3 scored" in result.output and "digest: 0 watchlist, 3 top" in result.output
    assert "email disabled" in result.output
    assert (reports / "digest-latest.html").exists()

    _seed(db_url, 2, start=10)
    result = invoke("run")
    assert result.exit_code == 0, result.output
    assert "digest: 0 watchlist, 2 top" in result.output  # the first 3 aren't repeated


def test_run_exits_1_when_email_fails_but_finishes(
    db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pipeline_env(tmp_path, monkeypatch)
    monkeypatch.setenv("JOB_SCOUT_DIGEST__EMAIL__ENABLED", "true")
    monkeypatch.setenv("JOB_SCOUT_DIGEST__EMAIL__FROM_ADDRESS", "a@example.test")
    monkeypatch.setenv("JOB_SCOUT_DIGEST__EMAIL__TO_ADDRESS", "b@example.test")
    monkeypatch.delenv("JOB_SCOUT_SMTP_TOKEN", raising=False)
    _seed(db_url, 1)
    result = invoke("run")
    assert result.exit_code == 1
    assert "1 scored" in result.output and "email failed" in result.output
    assert "JOB_SCOUT_SMTP_TOKEN is not set" in result.output
    assert "run finished with failures: digest" in result.output


def test_backlog_then_digest(db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reports = _pipeline_env(tmp_path, monkeypatch)
    _seed(db_url, 25)
    result = invoke("backlog", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "mode backlog" in result.output and "backlog report" not in result.output

    result = invoke("backlog", "--max-calls", "1")
    assert result.exit_code == 0, result.output
    assert "backlog report: 0 watchlist, 20 top" in result.output
    assert "5 job groups still pending" in result.output
    assert list(reports.glob("backlog-2*.html"))

    result = invoke("backlog")
    assert "backlog report: 0 watchlist, 25 top" in result.output  # cumulative

    result = invoke("digest")
    assert result.exit_code == 0, result.output
    assert "digest: 0 watchlist, 0 top" in result.output  # all already in a backlog report


def test_export_command(db_url: str, tmp_path: Path) -> None:
    _seed(db_url, 2)
    result = invoke("export", "--out", str(tmp_path / "exports"), "--sqlite")
    assert result.exit_code == 0, result.output
    assert "2 postings" in result.output and "database snapshot" in result.output
    assert len(list((tmp_path / "exports").glob("jobs-*.csv"))) == 1
    assert len(list((tmp_path / "exports").glob("job-scout-*.db"))) == 1
