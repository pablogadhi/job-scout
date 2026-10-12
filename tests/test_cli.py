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


def test_unimplemented_command_exits_1() -> None:
    result = invoke("score")
    assert result.exit_code == 1
    assert "Phase 3" in result.output


def test_record_refuses_tests_dir() -> None:
    result = invoke("fetch", "--record", "tests/fixtures/http")
    assert result.exit_code == 2
    assert "fixtures are synthetic" in result.output
