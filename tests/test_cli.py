from pathlib import Path

from typer.testing import CliRunner

from job_scout.cli import app
from tests.conftest import REPO_CONFIG, edit

runner = CliRunner()


def test_sources_check_lists_enabled_sources() -> None:
    result = runner.invoke(app, ["--config-dir", str(REPO_CONFIG), "sources", "check"])
    assert result.exit_code == 0, result.output
    assert "remoteok" in result.output
    assert "hn_whoishiring" in result.output
    assert "Canonical" in result.output


def test_sources_check_by_tier() -> None:
    result = runner.invoke(
        app, ["--config-dir", str(REPO_CONFIG), "sources", "check", "--tier", "1"]
    )
    assert result.exit_code == 0, result.output
    assert "Enabled sources (14)" in result.output
    table, disabled = result.output.split("disabled")
    assert "remoteok" in table
    assert "nodesk" not in table
    assert "nodesk" in disabled


def test_bad_config_exits_2(config_dir: Path) -> None:
    edit(config_dir / "settings.yaml", "backend: cli", "backend: gpt")
    result = runner.invoke(app, ["--config-dir", str(config_dir), "sources", "check"])
    assert result.exit_code == 2
    assert "scoring.backend" in result.output


def test_unimplemented_command_exits_1() -> None:
    result = runner.invoke(app, ["--config-dir", str(REPO_CONFIG), "fetch", "--tier", "1"])
    assert result.exit_code == 1
    assert "Phase 1" in result.output
