"""Digest, backlog report, email and export, on synthetic scored jobs (no network)."""

import csv
import sqlite3
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from job_scout.config import AppConfig, load_config
from job_scout.db.models import Digest, DigestKind, Job, Score, ScoreStatus
from job_scout.report.build import make_backlog_report, make_digest
from job_scout.report.data import build_sections, scored_items
from job_scout.report.export import export_csv, snapshot_sqlite
from job_scout.report.mailer import TOKEN_ENV
from job_scout.report.render import http_url
from tests.conftest import REPO_CONFIG
from tests.scoring_helpers import result_for, seed_jobs

NOW = datetime(2026, 10, 13, 14, 0, tzinfo=UTC)
EMAIL = {
    "enabled": True,
    "from_address": "job-scout@example.test",
    "to_address": "me@example.test",
}


def config_with(tmp_path: Path, **digest: Any) -> AppConfig:
    """The repo config, email off unless a test turns it on (whatever settings.yaml says)."""
    digest = {"email": {"enabled": False}, **digest}
    return load_config(
        REPO_CONFIG,
        {"paths": {"reports": str(tmp_path / "reports")}, "digest": digest},
    )


def score_jobs(sf: sessionmaker[Session], config: AppConfig, scores: dict[int, dict]) -> None:
    with sf.begin() as session:
        for job_id, fields in scores.items():
            result = result_for(job_id, **fields)
            session.add(
                Score(
                    job_id=job_id,
                    profile_hash=config.profile.sha256,
                    status=ScoreStatus.SCORED,
                    result=result,
                    overall_score=result["overall_score"],
                    disqualified=result["disqualified"],
                    scored_at=NOW,
                )
            )


class FakeSmtp:
    """Stands in for smtplib.SMTP; records what the mailer did."""

    instances: list["FakeSmtp"] = []

    def __init__(self, host: str, port: int, timeout: float, fail: Exception | None = None):
        self.host, self.port, self.fail = host, port, fail
        self.calls: list[str] = []
        self.login_args: tuple[str, str] | None = None
        self.messages: list[EmailMessage] = []
        FakeSmtp.instances.append(self)

    def __enter__(self) -> "FakeSmtp":
        return self

    def __exit__(self, *exc: object) -> None:
        self.calls.append("quit")

    def starttls(self, context: object) -> None:
        self.calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        if self.fail:
            raise self.fail
        self.calls.append("login")
        self.login_args = (user, password)

    def send_message(self, message: EmailMessage) -> None:
        self.messages.append(message)


def body_of(message: EmailMessage, subtype: str) -> str:
    part = message.get_body((subtype,))
    assert part is not None, f"no text/{subtype} part"
    return part.get_content()


@pytest.fixture(autouse=True)
def _reset_smtp() -> None:
    FakeSmtp.instances = []


def test_sections(tmp_path: Path, session_factory: sessionmaker[Session]) -> None:
    config = config_with(tmp_path, min_score=50, watchlist_min_score=21)
    a, b, c, d, e, f = seed_jobs(session_factory, 6)
    with session_factory.begin() as session:  # job f's company is on the watchlist
        session.execute(update(Job).where(Job.id == f).values(company_normalized="canonical"))
    score_jobs(
        session_factory,
        config,
        {
            a: {"score": 82},
            b: {"score": 55, "location_eligibility": "unclear"},
            c: {"score": 49},
            d: {"score": 10, "disqualified": True, "disqualify_reason": "US only"},
            e: {"score": 50},
            f: {"score": 30},
        },
    )
    with session_factory() as session:
        items = scored_items(session, {"canonical"})
    assert [i.job_id for i in items] == [a, b, e, c, f, d]  # best first
    sections = build_sections(items, config.settings.digest, include_below=True)
    assert [i.job_id for i in sections.watchlist] == [f]
    assert [i.job_id for i in sections.top] == [a, e]
    assert [i.job_id for i in sections.unclear] == [b]
    assert [i.job_id for i in sections.below] == [c, d]
    assert sorted(sections.job_ids) == sorted([a, b, e, f])  # below isn't "reported"


def test_digest_shows_only_new_matches(
    tmp_path: Path, session_factory: sessionmaker[Session]
) -> None:
    config = config_with(tmp_path, min_score=50)
    first, second = seed_jobs(session_factory, 2)
    score_jobs(session_factory, config, {first: {"score": 75}})

    one = make_digest(config, session_factory, now=NOW, environ={})
    assert one.recorded and one.emailed is None  # email disabled
    assert [i.job_id for i in one.sections.top] == [first]
    assert one.path.exists() and (one.path.parent / "digest-latest.html").exists()

    score_jobs(session_factory, config, {second: {"score": 60}})
    two = make_digest(config, session_factory, now=NOW + timedelta(days=1), environ={})
    assert [i.job_id for i in two.sections.top] == [second]  # first isn't repeated
    assert "new since" in two.path.read_text()

    three = make_digest(config, session_factory, now=NOW + timedelta(days=2), environ={})
    assert len(three.sections) == 0 and "Nothing new at or above 50" in three.path.read_text()


def test_backlog_report_includes_below_and_is_not_repeated(
    tmp_path: Path, session_factory: sessionmaker[Session]
) -> None:
    config = config_with(tmp_path, min_score=50)
    good, weak = seed_jobs(session_factory, 2)
    score_jobs(session_factory, config, {good: {"score": 70}, weak: {"score": 30}})
    report = make_backlog_report(config, session_factory, now=NOW, environ={})
    html = report.path.read_text()
    assert "Below threshold (1)" in html and report.path.name.startswith("backlog-")
    with session_factory() as session:
        row = session.scalars(select(Digest)).one()
    assert row.kind == DigestKind.BACKLOG and row.job_ids == [good]
    digest = make_digest(config, session_factory, now=NOW, environ={})
    assert len(digest.sections) == 0


def test_digest_email(tmp_path: Path, session_factory: sessionmaker[Session]) -> None:
    config = config_with(tmp_path, min_score=50, email=EMAIL)
    (job,) = seed_jobs(session_factory, 1)
    score_jobs(session_factory, config, {job: {"score": 80}})
    outcome = make_digest(
        config, session_factory, now=NOW, environ={TOKEN_ENV: "tok"}, smtp_factory=FakeSmtp
    )
    assert outcome.emailed is True and outcome.recorded
    (smtp,) = FakeSmtp.instances
    assert (smtp.host, smtp.port) == ("smtp.protonmail.ch", 587)
    assert smtp.calls == ["starttls", "login", "quit"]
    assert smtp.login_args == ("job-scout@example.test", "tok")
    (message,) = smtp.messages
    assert message["To"] == "me@example.test" and "1 top" in message["Subject"]
    assert body_of(message, "plain").count("[80]") == 1
    assert "Senior Backend Engineer 1" in body_of(message, "html")


def test_failed_email_is_not_recorded(
    tmp_path: Path, session_factory: sessionmaker[Session]
) -> None:
    config = config_with(tmp_path, min_score=50, email=EMAIL)
    (job,) = seed_jobs(session_factory, 1)
    score_jobs(session_factory, config, {job: {"score": 80}})

    missing = make_digest(config, session_factory, now=NOW, environ={})
    assert missing.emailed is False and TOKEN_ENV in (missing.email_error or "")

    import smtplib

    def refusing(host: str, port: int, timeout: float) -> FakeSmtp:
        return FakeSmtp(host, port, timeout, fail=smtplib.SMTPAuthenticationError(535, b"bad"))

    bad = make_digest(
        config, session_factory, now=NOW, environ={TOKEN_ENV: "x"}, smtp_factory=refusing
    )
    assert (
        bad.emailed is False
        and not bad.recorded
        and "SMTPAuthenticationError" in (bad.email_error or "")
    )
    assert "x" not in (bad.email_error or "").split()  # the token never shows up
    # Not recorded, so the job is still new for the next attempt.
    retry = make_digest(
        config, session_factory, now=NOW, environ={TOKEN_ENV: "x"}, smtp_factory=FakeSmtp
    )
    assert [i.job_id for i in retry.sections.top] == [job] and retry.recorded


def test_dry_run_records_and_sends_nothing(
    tmp_path: Path, session_factory: sessionmaker[Session]
) -> None:
    config = config_with(tmp_path, min_score=50, email=EMAIL)
    (job,) = seed_jobs(session_factory, 1)
    score_jobs(session_factory, config, {job: {"score": 80}})
    outcome = make_digest(
        config,
        session_factory,
        now=NOW,
        environ={TOKEN_ENV: "t"},
        dry_run=True,
        smtp_factory=FakeSmtp,
    )
    assert outcome.path.name == "digest-preview.html" and not FakeSmtp.instances
    with session_factory() as session:
        assert session.scalars(select(Digest)).first() is None


def test_empty_digest_email_can_be_skipped(
    tmp_path: Path, session_factory: sessionmaker[Session]
) -> None:
    config = config_with(tmp_path, email=EMAIL, send_when_empty=False)
    outcome = make_digest(
        config, session_factory, now=NOW, environ={TOKEN_ENV: "t"}, smtp_factory=FakeSmtp
    )
    assert outcome.skipped_empty and not FakeSmtp.instances and outcome.recorded


def test_posting_text_is_escaped_and_urls_checked(
    tmp_path: Path, session_factory: sessionmaker[Session]
) -> None:
    config = config_with(tmp_path, min_score=50)
    (job,) = seed_jobs(session_factory, 1)
    with session_factory.begin() as session:
        session.execute(
            update(Job)
            .where(Job.id == job)
            .values(title="<script>alert(1)</script>", apply_url="javascript:alert(1)")
        )
    score_jobs(session_factory, config, {job: {"score": 80, "summary": "<img src=x onerror=1>"}})
    html = make_digest(config, session_factory, now=NOW, environ={}).path.read_text()
    assert "<script>alert" not in html and "&lt;script&gt;" in html
    assert "<img src=x" not in html and "javascript:" not in html
    assert http_url(" https://a.example/x ") == "https://a.example/x"
    assert http_url("JavaScript:alert(1)") == "#" and http_url(None) == "#"


def test_export_csv_and_snapshot(
    tmp_path: Path, db_url: str, session_factory: sessionmaker[Session]
) -> None:
    config = config_with(tmp_path)
    first, second = seed_jobs(session_factory, 2)
    with session_factory.begin() as session:
        session.execute(update(Job).where(Job.id == second).values(title="=HYPERLINK(1)"))
    score_jobs(session_factory, config, {first: {"score": 77}})
    path = tmp_path / "out" / "jobs.csv"
    with session_factory() as session:
        assert export_csv(session, path) == 2
    rows = list(csv.DictReader(path.open()))
    assert rows[0]["score"] == "77" and rows[0]["prefilter"] == "passed"
    assert rows[0]["summary"] == "Synthetic posting that fits."
    assert rows[1]["title"] == "'=HYPERLINK(1)" and rows[1]["score"] == ""

    snapshot = snapshot_sqlite(db_url, tmp_path / "out" / "snap.db")
    with sqlite3.connect(snapshot) as conn:
        assert conn.execute("select count(*) from jobs").fetchone() == (2,)
    with pytest.raises(ValueError, match="SQLite"):
        snapshot_sqlite("postgresql+psycopg://h/db", tmp_path / "x.db")


def test_source_health_footer(
    app_config: AppConfig, session_factory: sessionmaker[Session]
) -> None:
    from job_scout.report.data import source_health

    with session_factory() as session:
        health = {h.source_id: h for h in source_health(session, app_config, {})}
        with_key = {
            h.source_id: h for h in source_health(session, app_config, {"FINDWORK_API_KEY": "k"})
        }
    assert "hiring_lat" not in health  # tier 2 (disabled)
    assert health["remoteintech"].problem == "never fetched"
    assert health["findwork"].problem == "skipped: FINDWORK_API_KEY not set"
    assert with_key["findwork"].problem == "never fetched"
    assert health["remoteok"].problem == "never fetched"


def test_token_never_shows_in_reprs() -> None:
    from job_scout.report.mailer import read_token

    token = read_token({TOKEN_ENV: "super-secret-token"})
    assert token is not None and token.get_secret_value() == "super-secret-token"
    assert "super-secret" not in repr(token) and "super-secret" not in str(token)
    assert read_token({}) is None


def test_recipients_from_env_and_several(
    tmp_path: Path, session_factory: sessionmaker[Session]
) -> None:
    no_recipient = config_with(tmp_path, email={**EMAIL, "to_address": None})
    outcome = make_digest(
        no_recipient, session_factory, now=NOW, environ={TOKEN_ENV: "t"}, smtp_factory=FakeSmtp
    )
    assert outcome.emailed is False and "JOB_SCOUT_DIGEST__EMAIL__TO_ADDRESS" in (
        outcome.email_error or ""
    )
    assert not outcome.recorded and not FakeSmtp.instances

    two = config_with(tmp_path, email={**EMAIL, "to_address": "a@example.test, b@example.test"})
    assert two.settings.digest.email.recipients == ["a@example.test", "b@example.test"]
    outcome = make_digest(
        two, session_factory, now=NOW, environ={TOKEN_ENV: "t"}, smtp_factory=FakeSmtp
    )
    assert outcome.emailed is True
    assert FakeSmtp.instances[0].messages[0]["To"] == "a@example.test, b@example.test"


def test_failure_report_redacts_and_trims() -> None:
    from job_scout.report.failure import FailureReport, log_tail, redact

    assert redact("key sk-ant-api03-abcdefghijkl was rejected") == "key [redacted] was rejected"
    assert redact("JOB_SCOUT_SMTP_TOKEN=s3cret ok") == "[redacted] ok"
    tail = log_tail("\n".join(f"line {i}" for i in range(500)))
    assert tail.splitlines()[0] == "line 380" and tail.endswith("line 499")
    timeout = FailureReport("job-scout.service", "timeout", "15", "")
    assert timeout.subject == "job-scout: daily run timed out"
    assert "(no log captured)" in timeout.text(NOW)


def test_notify_failure_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from job_scout.cli import app

    monkeypatch.setattr("job_scout.report.mailer._smtp", FakeSmtp)
    env = {
        "COLUMNS": "200",
        TOKEN_ENV: "tok",
        "JOB_SCOUT_DIGEST__EMAIL__ENABLED": "true",
        "JOB_SCOUT_DIGEST__EMAIL__FROM_ADDRESS": "job-scout@example.test",
        "JOB_SCOUT_DIGEST__EMAIL__TO_ADDRESS": "me@example.test",
    }
    log = "fetch ok\nscoring failed: CLAUDE_CODE_OAUTH_TOKEN=abc123 expired\n"
    args = ["--config-dir", str(REPO_CONFIG), "notify-failure", "--unit", "job-scout.service"]
    result = CliRunner().invoke(
        app, [*args, "--result", "exit-code", "--exit-status", "1"], input=log, env=env
    )
    assert result.exit_code == 0, result.output
    (message,) = FakeSmtp.instances[0].messages
    assert message["Subject"] == "job-scout: daily run failed (exit-code)"
    body = body_of(message, "plain")
    assert "scoring failed: [redacted] expired" in body and "abc123" not in body

    off = CliRunner().invoke(
        app, args, input=log, env={**env, "JOB_SCOUT_DIGEST__EMAIL__ENABLED": "false"}
    )
    assert off.exit_code == 1 and "email is disabled" in off.output
    assert "Last log lines" in off.output  # still printed, for the journal
