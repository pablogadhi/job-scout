from collections.abc import Callable
from datetime import timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from job_scout.config import AppConfig
from job_scout.db.models import Job, Source, SourceRun, SourceRunKind, SourceRunStatus
from job_scout.db.types import utcnow
from job_scout.fetch.http import HttpClient
from job_scout.fetch.runner import SourceOutcome, run_fetch

RSS_SOURCES = ["pyjobs", "typescriptjobs", "remotegamejobs"]


def by_id(outcomes: list[SourceOutcome]) -> dict[str, SourceOutcome]:
    return {o.source_id: o for o in outcomes}


def test_second_run_adds_nothing(
    app_config: AppConfig,
    session_factory: sessionmaker[Session],
    make_http: Callable[..., HttpClient],
) -> None:
    with make_http() as http:
        first = by_id(run_fetch(app_config, session_factory, http, source_ids=RSS_SOURCES))
        assert all(o.status == "ok" and o.new > 0 for o in first.values())
        second = by_id(
            run_fetch(app_config, session_factory, http, source_ids=RSS_SOURCES, force=True)
        )
    for sid in RSS_SOURCES:
        assert (second[sid].new, second[sid].duplicate) == (0, 0)
        assert second[sid].seen == first[sid].fetched
    with session_factory() as session:
        state = session.get_one(Source, "pyjobs")
        assert state.last_item_count == first["pyjobs"].fetched
        assert state.consecutive_failures == 0
        assert (
            session.scalar(
                select(SourceRun.items_new).where(SourceRun.source_id == "pyjobs").limit(1)
            )
            > 0
        )


def test_min_interval_skips_without_force(
    app_config: AppConfig,
    session_factory: sessionmaker[Session],
    make_http: Callable[..., HttpClient],
) -> None:
    with make_http() as http:
        run_fetch(app_config, session_factory, http, source_ids=["pyjobs"])
        again = run_fetch(app_config, session_factory, http, source_ids=["pyjobs"])[0]
    assert again.status == "skipped"
    assert "min interval 6h" in again.reason


def test_one_failing_source_does_not_stop_others(
    app_config: AppConfig,
    session_factory: sessionmaker[Session],
    make_http: Callable[..., HttpClient],
) -> None:
    broken = {"https://typescriptjobs.net/feed.xml": lambda _r: httpx.Response(503)}
    with make_http(broken) as http:
        outcomes = by_id(run_fetch(app_config, session_factory, http, source_ids=RSS_SOURCES))
    assert outcomes["typescriptjobs"].status == "failed"
    assert "HTTP 503" in outcomes["typescriptjobs"].reason
    assert outcomes["pyjobs"].status == outcomes["remotegamejobs"].status == "ok"
    with session_factory() as session:
        state = session.get_one(Source, "typescriptjobs")
        assert state.consecutive_failures == 1 and state.last_success_at is None
        run = session.scalars(
            select(SourceRun).where(SourceRun.source_id == "typescriptjobs")
        ).one()
        assert run.status == SourceRunStatus.FAILED and "503" in run.error


def test_html_instead_of_feed_fails(
    app_config: AppConfig,
    session_factory: sessionmaker[Session],
    make_http: Callable[..., HttpClient],
) -> None:
    html = {
        "https://www.pyjobs.com/rss": lambda _r: httpx.Response(
            200, headers={"content-type": "text/html"}, text="<!DOCTYPE html><html><body><p>hi"
        )
    }
    with make_http(html) as http:
        outcome = run_fetch(app_config, session_factory, http, source_ids=["pyjobs"])[0]
    assert outcome.status == "failed" and "not a valid feed" in outcome.reason


def test_request_budget_skips_remotive(
    app_config: AppConfig,
    session_factory: sessionmaker[Session],
    make_http: Callable[..., HttpClient],
) -> None:
    with session_factory.begin() as session:
        session.add(Source(id="remotive"))
        session.flush()
        session.add(
            SourceRun(
                source_id="remotive",
                status=SourceRunStatus.OK,
                requests_made=4,
                started_at=utcnow() - timedelta(hours=3),
            )
        )
    with make_http() as http:
        outcome = run_fetch(app_config, session_factory, http, source_ids=["remotive"], force=True)[
            0
        ]
    assert outcome.status == "skipped"
    assert outcome.reason == "request budget: 4 of 4 used in the last 24h"


def test_remotive_first_run_checks_categories(
    app_config: AppConfig,
    session_factory: sessionmaker[Session],
    make_http: Callable[..., HttpClient],
) -> None:
    with make_http() as http:
        outcome = run_fetch(app_config, session_factory, http, source_ids=["remotive"])[0]
    assert outcome.status == "ok"
    assert outcome.requests == 2  # /categories (first run only) + one jobs endpoint


def test_missing_credentials_and_unbuilt_methods_skip(
    app_config: AppConfig,
    session_factory: sessionmaker[Session],
    make_http: Callable[..., HttpClient],
) -> None:
    with make_http() as http:
        outcomes = by_id(
            run_fetch(
                app_config,
                session_factory,
                http,
                source_ids=["findwork", "remoteintech", "nodesk"],
                env={},
            )
        )
    assert outcomes["findwork"].reason == "FINDWORK_API_KEY is not set"
    assert "Phase 5" in outcomes["remoteintech"].reason
    assert "Phase 7" in outcomes["nodesk"].reason
    assert {o.status for o in outcomes.values()} == {"skipped"}


def test_probe_stores_nothing(
    app_config: AppConfig,
    session_factory: sessionmaker[Session],
    make_http: Callable[..., HttpClient],
) -> None:
    with make_http() as http:
        outcome = run_fetch(app_config, session_factory, http, source_ids=["pyjobs"], probe=True)[0]
    assert outcome.status == "ok" and outcome.fetched > 0 and outcome.new == 0
    with session_factory() as session:
        assert session.scalar(select(Job.id).limit(1)) is None
        assert session.get_one(Source, "pyjobs").last_run_at is None
        assert session.scalars(select(SourceRun.kind)).all() == [SourceRunKind.PROBE]


def test_tier_1_end_to_end(
    app_config: AppConfig,
    session_factory: sessionmaker[Session],
    make_http: Callable[..., HttpClient],
) -> None:
    with make_http() as http:
        outcomes = by_id(run_fetch(app_config, session_factory, http, tier=1))
    stored = {sid for sid, o in outcomes.items() if o.status == "ok" and o.new > 0}
    assert stored == {
        "4dayweek", "remoteok", "weworkremotely", "remotive", "workingnomads",
        "realworkfromanywhere", "remotefirstjobs", "hn_whoishiring", "pyjobs",
        "typescriptjobs", "remotegamejobs", "findjobit",
    }  # fmt: skip
    assert {outcomes["findwork"].status, outcomes["remoteintech"].status} == {"skipped"}
