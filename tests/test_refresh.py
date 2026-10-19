"""`job-scout refresh`: a mapper fix applied to a source's stored jobs (synthetic jobs)."""

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from job_scout.cli import app
from job_scout.db.models import Job, PrefilterResult, Score, ScoreStatus, Source
from job_scout.fetch.http import HttpClient
from tests.conftest import TEST_CONFIG, fixture_transport
from tests.scoring_helpers import T0, result_for, with_points

PYJOBS_URL = "https://www.pyjobs.com/job/senior-python-engineer-Qz12Ab34"
FOURDAYWEEK_ITEM = {
    "id": "4dw-0099",
    "url": "https://4dayweek.io/job/senior-backend-engineer-at-tidewater-0099",
    "title": "Senior Backend Engineer",
    "company": {"name": "Tidewater"},
    "description": "Python services.",
    "work_arrangement": "remote",
    "locations": [
        {"country": "United States", "continent": "North America", "work_arrangement": "remote"},
        {"country": "Canada", "continent": "North America", "work_arrangement": "remote"},
    ],
}


def _store(
    sf: sessionmaker[Session], source: str, source_job_id: str, url: str, raw: dict[str, Any], **job
) -> int:
    """A job stored before the fix: judged, passed and scored."""
    with sf.begin() as session:
        if session.get(Source, source) is None:
            session.add(Source(id=source))
            session.flush()
        stored = Job(
            source_id=source,
            source_job_id=source_job_id,
            canonical_url=url,
            company="Tidewater",
            company_normalized="tidewater",
            title="Senior Python Engineer",
            title_normalized="senior python engineer",
            first_seen_at=T0,
            last_seen_at=T0,
            raw=raw,
            dedupe_key=f"{source}-{source_job_id}",
            **job,
        )
        session.add(stored)
        session.flush()
        session.add(PrefilterResult(job_id=stored.id, passed=True, reason=None, fingerprint="f"))
        score = Score(job_id=stored.id, profile_hash="p", status=ScoreStatus.SCORED)
        session.add(with_points(score, result_for(stored.id, score=75)))
        return stored.id


def _http(overrides: dict[str, Callable[[httpx.Request], httpx.Response]] | None = None):
    def factory(defaults: Any) -> HttpClient:
        return HttpClient(defaults, transport=fixture_transport(overrides), sleep=lambda _s: None)

    return factory


def _refresh(*args: str) -> str:
    result = CliRunner().invoke(
        app, ["--config-dir", str(TEST_CONFIG), "refresh", *args], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.output
    return result.output


def _state(sf: sessionmaker[Session], job_id: int) -> tuple[str | None, bool, ScoreStatus]:
    with sf() as session:
        job = session.get_one(Job, job_id)
        verdict = session.get(PrefilterResult, job_id) is not None
        score = session.scalars(select(Score).where(Score.job_id == job_id)).one()
        return job.location_raw, verdict, score.status


def test_refresh_maps_stored_jobs_again_and_queues_them(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"request: {request.url}")

    monkeypatch.setattr("job_scout.cli.sources.HttpClient", _http({"https://": no_request}))
    job_id = _store(
        session_factory,
        "4dayweek",
        "4dw-0099",
        FOURDAYWEEK_ITEM["url"],
        FOURDAYWEEK_ITEM,
        location_raw="United States (remote); Canada (remote)",
        remote_raw="remote",
        description_text="Python services.",
    )

    output = _refresh("--source", "4dayweek", "--dry-run")
    assert "1 changed by mapping again" in output and "nothing saved" in output
    before = ("United States (remote); Canada (remote)", True, ScoreStatus.SCORED)
    assert _state(session_factory, job_id) == before

    output = _refresh("--source", "4dayweek")
    assert "1 job groups to filter again, 1 scores queued again" in output
    # The verdict is gone (the next `filter` judges the group again) and the score waits.
    location = "remote, must be based in United States or Canada"
    assert _state(session_factory, job_id) == (location, False, ScoreStatus.PENDING)

    output = _refresh("--source", "4dayweek")
    assert "0 changed by mapping again" in output


def test_refresh_reads_the_pages_stored_jobs_lack(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("job_scout.cli.sources.HttpClient", _http())
    entry = {
        "title": "Senior Python Engineer",
        "link": PYJOBS_URL,
        "summary": "Senior Python Engineer at Quetzal Labs. Geo remote · Full-time.",
    }
    job_id = _store(
        session_factory,
        "pyjobs",
        PYJOBS_URL,
        PYJOBS_URL,
        entry,
        remote_raw="Geo remote",
        description_text=entry["summary"],
    )
    output = _refresh("--source", "pyjobs")
    assert "1 job pages read (0 failed, 1 requests)" in output
    location = "must be based in Canada or Mexico"
    assert _state(session_factory, job_id) == (location, False, ScoreStatus.PENDING)
    with session_factory() as session:
        job = session.get_one(Job, job_id)
        assert job.description_text and job.description_text.startswith("Build our Django APIs.")
        assert job.raw["page"]["jobLocationType"] == "TELECOMMUTE"

    def no_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"request: {request.url}")

    monkeypatch.setattr("job_scout.cli.sources.HttpClient", _http({"https://": no_request}))
    assert "0 job pages read" in _refresh("--source", "pyjobs")  # read once, kept


@pytest.mark.usefixtures("db_url")
def test_refresh_refuses_sources_it_cannot_map() -> None:
    result = CliRunner().invoke(
        app, ["--config-dir", str(TEST_CONFIG), "refresh", "--source", "hn_whoishiring"]
    )
    assert result.exit_code != 0
    assert "can't be mapped again" in result.output
