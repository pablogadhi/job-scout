"""The deploy files stay consistent with the code and config (no Docker or systemd needed)."""

import re
from pathlib import Path

import yaml

from job_scout.config import AppConfig
from job_scout.feedback.links import SECRET_ENV
from job_scout.report import mailer
from tests.conftest import ROOT

DEPLOY = ROOT / "deploy"


def unit(name: str) -> str:
    return (DEPLOY / "systemd" / name).read_text()


def test_timer_matches_daily_run_time(app_config: AppConfig) -> None:
    schedule = app_config.settings.schedule
    match = re.search(r"^OnCalendar=\*-\*-\* (\d\d:\d\d):00 (\S+)$", unit("job-scout.timer"), re.M)
    assert match, "OnCalendar must be a daily time with a timezone"
    assert match.group(1) == schedule.daily_run_time.strftime("%H:%M")
    assert match.group(2) == schedule.timezone
    assert "Persistent=true" in unit("job-scout.timer")


def test_service_reports_failures_and_hangs() -> None:
    service = unit("job-scout.service")
    assert "OnFailure=job-scout-failure.service" in service
    assert re.search(r"^TimeoutStartSec=\S+", service, re.M)
    assert "docker compose run --rm -T job-scout run" in service
    failure = unit("job-scout-failure.service")
    assert "notify-failure" in failure and "$MONITOR_INVOCATION_ID" in failure
    assert "notify-failure" in (DEPLOY / "entrypoint.sh").read_text()  # skips migrations


def test_compose_mounts_the_configured_paths(app_config: AppConfig) -> None:
    compose = yaml.safe_load((DEPLOY / "compose.yaml").read_text())["services"]["job-scout"]
    data_mount = next(v for v in compose["volumes"] if v.endswith(":/var/lib/job-scout"))
    assert data_mount.startswith("/var/lib/job-scout:")
    paths = app_config.settings.paths
    for path in (
        paths.database,
        paths.reports,
        paths.exports,
        app_config.settings.scoring.cli.workdir,
    ):
        assert Path(path).is_relative_to("/var/lib/job-scout"), path
    assert "../config:/app/config:ro" in compose["volumes"]


def test_feedback_page_service(app_config: AppConfig) -> None:
    services = yaml.safe_load((DEPLOY / "compose.yaml").read_text())["services"]
    page = services["feedback"]
    assert page["command"] == ["feedback", "serve"]
    assert page["restart"] == "unless-stopped"
    assert page["volumes"] == services["job-scout"]["volumes"]
    assert page["environment"]["JOB_SCOUT_FEEDBACK__HOST"] == "0.0.0.0"
    port = app_config.settings.feedback.port
    assert any(p.endswith(f":{port}") for p in page["ports"])


def test_env_example_lists_every_secret() -> None:
    names = set(
        re.findall(
            r"^#?\s*([A-Z][A-Z0-9_]+)=", (DEPLOY / "job-scout.env.example").read_text(), re.M
        )
    )
    expected = {
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        mailer.TOKEN_ENV,
        mailer.TO_ENV,
        SECRET_ENV,
        "JOB_SCOUT_FEEDBACK__BASE_URL",
        "FINDWORK_API_KEY",
    }
    assert expected <= names


def test_secrets_never_enter_the_image() -> None:
    ignored = (ROOT / ".dockerignore").read_text().split()
    assert {".env", ".env.*", "data", "*.db"} <= set(ignored)
