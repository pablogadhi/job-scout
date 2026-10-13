import json
import logging
import os
import shutil
import smtplib
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from job_scout.config import AppConfig, load_config
from job_scout.db import make_engine, make_session_factory
from job_scout.fetch.http import HttpClient

ROOT = Path(__file__).resolve().parent.parent
REPO_CONFIG = ROOT / "config"
HTTP_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "http"


KEEP_ENV = {"JOB_SCOUT_SMTP_TOKEN"}


@pytest.fixture(autouse=True)
def _no_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Offline tests can't reach the network: a real HTTP request or SMTP connection fails
    the test at once.

    Fixture transports (httpx.MockTransport) are unaffected; `-m live` tests are exempt.
    """
    if request.node.get_closest_marker("live"):
        return

    def refuse(self: object, request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"offline test tried the network: {request.method} {request.url}")

    def refuse_smtp(self: object, host: str = "", *args: object, **kwargs: object) -> None:
        raise AssertionError(f"offline test tried to connect to SMTP server {host!r}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
    monkeypatch.setattr(smtplib.SMTP, "connect", refuse_smtp)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must not see JOB_SCOUT_* settings from the developer's shell. Secrets that
    aren't settings stay, for the opt-in live tests; offline tests pass their own env."""
    for var in list(os.environ):
        if var.startswith("JOB_SCOUT_") and var not in KEEP_ENV:
            monkeypatch.delenv(var)


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """A writable copy of the real config/ directory."""
    target = tmp_path / "config"
    shutil.copytree(REPO_CONFIG, target, copy_function=shutil.copyfile)  # drop read-only modes
    return target


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, f"{old!r} not in {path.name}"
    path.write_text(text.replace(old, new, 1))


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """CLI tests call setup_logging, which binds handlers to CliRunner's temporary stderr."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.fixture
def db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A migrated SQLite database; also exported so CLI commands use it."""
    url = f"sqlite:///{tmp_path / 'db' / 'test.db'}"
    cfg = Config(ROOT / "alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")
    monkeypatch.setenv("JOB_SCOUT_DATABASE_URL", url)
    return url


@pytest.fixture
def engine(db_url: str) -> Iterator[Engine]:
    engine = make_engine(db_url)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return make_session_factory(engine)


@pytest.fixture
def app_config() -> AppConfig:
    return load_config(REPO_CONFIG)


def load_http_fixtures() -> dict[str, httpx.Response]:
    """URL -> recorded response, from every tests/fixtures/http/*/manifest.json."""
    responses = {}
    for manifest in HTTP_FIXTURES.glob("*/manifest.json"):
        for url, entry in json.loads(manifest.read_text()).items():
            body = (manifest.parent / entry["file"]).read_bytes()
            responses[url] = (entry["status"], entry["content_type"], body)
    return responses


Handler = Callable[[httpx.Request], httpx.Response]


def fixture_transport(overrides: dict[str, Handler] | None = None) -> httpx.MockTransport:
    """Replays recorded responses. robots.txt is absent (allow all) unless overridden.

    An unrecorded URL fails the test, so a fetcher can never silently hit the network.
    """
    recorded = load_http_fixtures()
    overrides = overrides or {}

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for prefix, handler in overrides.items():
            if url.startswith(prefix):
                return handler(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if url not in recorded:
            raise AssertionError(f"unrecorded request: {url}")
        status, content_type, body = recorded[url]
        return httpx.Response(status, headers={"content-type": content_type}, content=body)

    return httpx.MockTransport(handle)


@pytest.fixture
def make_http(app_config: AppConfig) -> Callable[..., HttpClient]:
    """HttpClient over the fixture transport. Sleeps are skipped unless `sleep` is given."""

    def factory(
        overrides: dict[str, Handler] | None = None,
        sleep: Callable[[float], None] = lambda _seconds: None,
    ) -> HttpClient:
        return HttpClient(
            app_config.sources.defaults, transport=fixture_transport(overrides), sleep=sleep
        )

    return factory
