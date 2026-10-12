from pathlib import Path

import pytest
from typer.testing import CliRunner

from job_scout.cli import app
from tests.conftest import REPO_CONFIG, edit

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
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from job_scout.db import make_engine, make_session_factory
    from job_scout.db.models import Job, Source
    from job_scout.fetch.models import Posting
    from job_scout.fetch.store import store_postings

    engine = make_engine(db_url)
    sf = make_session_factory(engine)
    now = datetime(2026, 10, 12, tzinfo=UTC)
    with sf.begin() as session:
        session.add_all([Source(id="a"), Source(id="b")])
        session.flush()
        for source, job_id, location, when in (
            ("a", "1", "EMEA", now),
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
    assert "a: EMEA" in result.output and "b: Latin America" in result.output
    assert f"via copy #{copy_id} on b" in result.output
    assert "none yet" in result.output  # no scores before Phase 3


@pytest.mark.usefixtures("db_url")
def test_explain_unknown_job_exits_1() -> None:
    result = invoke("explain", "999")
    assert result.exit_code == 1
    assert "no job with id 999" in result.output


def test_unimplemented_command_exits_1() -> None:
    result = invoke("score")
    assert result.exit_code == 1
    assert "Phase 3" in result.output


def test_record_refuses_tests_dir() -> None:
    result = invoke("fetch", "--record", "tests/fixtures/http")
    assert result.exit_code == 2
    assert "fixtures are synthetic" in result.output
