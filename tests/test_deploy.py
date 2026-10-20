"""The deploy files stay consistent with the code and config (no Docker or systemd needed)."""

import re

import pytest
import yaml

from job_scout.config import AppConfig, Settings, load_config, load_settings_only
from job_scout.feedback.links import SECRET_ENV
from job_scout.report import mailer
from tests.conftest import REPO_CONFIG, ROOT

DEPLOY = ROOT / "deploy"
INSTANCES = ROOT / "instances"
# compose's path to the instance's directory: the repo root unless an instance's .env sets it.
INSTANCE_DIR = "${JOB_SCOUT_INSTANCE_DIR:-..}"


@pytest.fixture
def app_config() -> AppConfig:
    """The real config/: the deploy files must agree with the settings that get deployed."""
    return load_config(REPO_CONFIG)


def unit(name: str) -> str:
    return (DEPLOY / "systemd" / name).read_text()


def instance_names() -> list[str]:
    """Instances with a timer in deploy/systemd (job-scout@<name>.timer)."""
    timers = (DEPLOY / "systemd").glob("job-scout@*.timer")
    return sorted(p.name.removeprefix("job-scout@").removesuffix(".timer") for p in timers)


def assert_timer_matches(timer: str, settings: Settings) -> None:
    schedule = settings.schedule
    match = re.search(r"^OnCalendar=\*-\*-\* (\d\d:\d\d):00 (\S+)$", timer, re.M)
    assert match, "OnCalendar must be a daily time with a timezone"
    assert match.group(1) == schedule.daily_run_time.strftime("%H:%M")
    assert match.group(2) == schedule.timezone
    assert "Persistent=true" in timer


def assert_data_paths_relative(settings: Settings) -> None:
    paths = settings.paths
    for path in (paths.database, paths.reports, paths.exports, settings.scoring.cli.workdir):
        assert not path.is_absolute() and path.is_relative_to("data"), path


def test_timer_matches_daily_run_time(app_config: AppConfig) -> None:
    assert_timer_matches(unit("job-scout.timer"), app_config.settings)


@pytest.mark.parametrize("name", instance_names())
def test_instance_timer_matches_its_settings(name: str) -> None:
    """Each instance's timer against its own settings.yaml, where the instance exists."""
    config_dir = INSTANCES / name / "config"
    if not config_dir.is_dir():
        pytest.skip(f"instances/{name} isn't on this machine (instances/ is gitignored)")
    settings = load_settings_only(config_dir)
    assert_timer_matches(unit(f"job-scout@{name}.timer"), settings)
    assert_data_paths_relative(settings)


def test_instance_service_is_yours_plus_env_file_and_ordering() -> None:
    """job-scout@.service differs from job-scout.service only in selecting the instance and
    running after yours (never two instances fetching at once), so a fix to one can't be
    forgotten in the other. Failures of every instance go to your failure service (your
    recipient, never the instance's)."""

    def body(text: str) -> list[str]:
        return [
            line
            for line in text.splitlines()
            if line and not line.startswith(("#", "Description="))
        ]

    selected = "docker compose --env-file ../instances/%i/.env run"
    expected = [
        line.replace("docker compose run", selected) for line in body(unit("job-scout.service"))
    ]
    expected.insert(
        expected.index("OnFailure=job-scout-failure.service") + 1, "After=job-scout.service"
    )
    assert body(unit("job-scout@.service")) == expected


def test_service_reports_failures_and_hangs() -> None:
    service = unit("job-scout.service")
    assert "OnFailure=job-scout-failure.service" in service
    assert re.search(r"^TimeoutStartSec=\S+", service, re.M)
    assert "docker compose run --rm -T job-scout run" in service
    failure = unit("job-scout-failure.service")
    assert "notify-failure" in failure and "$MONITOR_INVOCATION_ID" in failure
    assert "notify-failure" in (DEPLOY / "entrypoint.sh").read_text()  # skips migrations


def test_services_find_docker_wherever_it_is_installed() -> None:
    """The snap installs docker in /snap/bin, outside systemd's default search path."""
    for name in ("job-scout.service", "job-scout@.service", "job-scout-failure.service"):
        service = unit(name)
        assert re.search(r"^ExecSearchPath=\S*:/snap/bin$", service, re.M), name
        assert "/usr/bin/docker" not in service, name


def test_compose_mounts_the_configured_paths(app_config: AppConfig) -> None:
    compose = yaml.safe_load((DEPLOY / "compose.yaml").read_text())["services"]["job-scout"]
    assert compose["env_file"] == f"{INSTANCE_DIR}/.env"
    assert f"{INSTANCE_DIR}/data:/app/data" in compose["volumes"]
    assert f"{INSTANCE_DIR}/config:/app/config:ro" in compose["volumes"]
    assert_data_paths_relative(app_config.settings)
    assert "HOME=/app/data/home" in (ROOT / "Dockerfile").read_text()


def test_deploy_stays_in_the_users_home() -> None:
    """No system paths: the repo, data and secrets all live in ~/job-scout."""
    files = [ROOT / "Dockerfile", *(p for p in DEPLOY.rglob("*") if p.is_file())]
    for path in files:
        text = path.read_text(encoding="utf-8")
        for system_path in ("/var/lib", "/opt/", "/etc/job-scout", "/etc/systemd"):
            assert system_path not in text, f"{path.name} mentions {system_path}"
    for name in ("job-scout.service", "job-scout@.service", "job-scout-failure.service"):
        assert "WorkingDirectory=%h/job-scout/deploy" in unit(name)
    assert "journalctl --user" in unit("job-scout-failure.service")


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
    names = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", (ROOT / ".env.example").read_text(), re.M))
    expected = {
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        mailer.TOKEN_ENV,
        mailer.TO_ENV,
        SECRET_ENV,
        "JOB_SCOUT_FEEDBACK__BASE_URL",
        "JOB_SCOUT_SCORING__CLI__TOKEN_CREATED",
        "FINDWORK_API_KEY",
    }
    assert expected <= names


# Names that look like our env vars: JOB_SCOUT_*, or *_API_KEY / *_TOKEN / *_SECRET. In
# Python only quoted ones count for the latter (CHARS_PER_TOKEN is a constant, not a var).
_PREFIXED = re.compile(r"\bJOB_SCOUT_[A-Z0-9_]*[A-Z0-9]\b")
_SECRET_LIKE = r"[A-Z][A-Z0-9_]*_(?:API_KEY|TOKEN|SECRET)"
_QUOTED_SECRET = re.compile(rf"[\"']({_SECRET_LIKE})[\"']")
_BARE_SECRET = re.compile(rf"\b({_SECRET_LIKE})\b")


def test_dotenv_example_lists_every_env_var() -> None:
    """Every env var named in the code, config or deploy files is in .env.example."""
    used: set[str] = set()
    for path in [*(ROOT / "src").rglob("*.py"), ROOT / "Dockerfile"]:
        text = path.read_text(encoding="utf-8")
        used |= set(_PREFIXED.findall(text)) | set(_QUOTED_SECRET.findall(text))
    for path in [*(ROOT / "config").glob("*"), *(p for p in DEPLOY.rglob("*") if p.is_file())]:
        text = path.read_text(encoding="utf-8")
        used |= set(_PREFIXED.findall(text)) | set(_BARE_SECRET.findall(text))
    listed = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", (ROOT / ".env.example").read_text(), re.M))
    assert used - listed == set()


def test_secrets_never_enter_the_image() -> None:
    ignored = (ROOT / ".dockerignore").read_text().split()
    assert {".env", ".env.*", "data", "*.db", "instances"} <= set(ignored)
    assert "/instances/" in (ROOT / ".gitignore").read_text().split()
