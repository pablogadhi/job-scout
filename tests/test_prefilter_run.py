"""Applying the pre-filter to stored jobs: groups, re-evaluation, report."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from job_scout.config import AppConfig
from job_scout.db.groups import group_of
from job_scout.db.models import Job, PrefilterResult, Source
from job_scout.fetch.http import HttpClient
from job_scout.fetch.models import Posting
from job_scout.fetch.runner import run_fetch
from job_scout.fetch.store import store_postings
from job_scout.prefilter.runner import drop_reasons, run_prefilter, source_report

T0 = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
KEY = ["company_normalized", "title_normalized"]


def posting(job_id: str, location: str) -> Posting:
    return Posting(
        source_job_id=job_id,
        canonical_url=f"https://board.example/{job_id}",
        title="Senior Platform Engineer",
        company="Northwind Cloud",
        location_raw=location,
        raw={"id": job_id},
    )


def store(
    sf: sessionmaker[Session], source: str, job_id: str, location: str, when: datetime
) -> None:
    with sf.begin() as session:
        if session.get(Source, source) is None:
            session.add(Source(id=source))
            session.flush()
        store_postings(session, source, [posting(job_id, location)], KEY, when)


def verdict_for(sf: sessionmaker[Session], source_job_id: str) -> PrefilterResult:
    with sf() as session:
        job = session.scalars(select(Job).where(Job.source_job_id == source_job_id)).one()
        return session.get_one(PrefilterResult, job.duplicate_of or job.id)


def test_group_reevaluated_when_eligible_copy_arrives(
    app_config: AppConfig, session_factory: sessionmaker[Session]
) -> None:
    store(session_factory, "a", "emea", "EMEA only", T0)
    assert run_prefilter(app_config, session_factory).evaluated == 1
    assert not verdict_for(session_factory, "emea").passed

    # Nothing new: nothing to evaluate.
    assert run_prefilter(app_config, session_factory).evaluated == 0

    # The same job shows up later for Latin America on another board.
    store(session_factory, "b", "latam", "Latin America", datetime.now(UTC) + timedelta(minutes=1))
    assert run_prefilter(app_config, session_factory).evaluated == 1
    verdict = verdict_for(session_factory, "emea")
    assert verdict.passed and verdict.group_size == 2
    assert "via copy" in verdict.reason
    with session_factory() as session:
        assert (
            session.scalar(
                select(PrefilterResult.job_id).where(PrefilterResult.job_id != verdict.job_id)
            )
            is None
        )


def test_rule_change_and_rerun_reevaluate(
    app_config: AppConfig, session_factory: sessionmaker[Session]
) -> None:
    store(session_factory, "a", "1", "Worldwide", T0)
    run_prefilter(app_config, session_factory)
    assert run_prefilter(app_config, session_factory, rerun=True).evaluated == 1

    stricter = app_config.filters.model_copy(
        update={"salary": app_config.filters.salary.model_copy(update={"min_annual": 130_000})}
    )
    changed = AppConfig(
        app_config.config_dir, app_config.settings, app_config.sources, stricter, app_config.profile
    )
    assert run_prefilter(changed, session_factory).evaluated == 1


def test_combined_locations(app_config: AppConfig, session_factory: sessionmaker[Session]) -> None:
    store(session_factory, "a", "de", "Germany (remote)", T0)
    store(session_factory, "b", "mx", "Mexico (remote)", T0 + timedelta(hours=1))
    store(session_factory, "b", "mx2", "Mexico (remote)", T0 + timedelta(hours=2))
    with session_factory() as session:
        copy_id = session.scalars(select(Job.id).where(Job.source_job_id == "mx")).one()
        group = group_of(session, copy_id)
        assert group is not None and len(group.jobs) == 3
        assert group.combined_locations() == ["a: Germany (remote)", "b: Mexico (remote)"]


def test_synthetic_fixtures_end_to_end(
    app_config: AppConfig,
    session_factory: sessionmaker[Session],
    make_http: Callable[..., HttpClient],
) -> None:
    with make_http() as http:
        run_fetch(app_config, session_factory, http, tier=1)
    run_prefilter(app_config, session_factory)

    expected = {
        # (source, title): passed, start of reason
        ("remoteok", "Marketing Manager"): (False, "title: matches no include"),
        ("4dayweek", "Backend Engineer (Python)"): (False, "remote: '\\\\bhybrid\\\\b'"),
        ("4dayweek", "Staff Software Engineer"): (True, "location allowed"),  # $9-11k/month
        ("weworkremotely", "Full-Stack Developer"): (False, "location:"),  # USA Only
        ("remotefirstjobs", "Senior DevOps Engineer"): (False, "salary: top of range 115,000"),
        ("pyjobs", "Data Engineer Intern"): (False, "title: excluded by"),
        ("pyjobs", "Backend Engineer (R&D)"): (False, "remote:"),  # Hybrid
        ("findjobit", "Senior Backend Engineer"): (True, "location allowed ('Guatemala'"),
        ("remotegamejobs", "Senior Backend Engineer - Online Services"): (True, "location unclear"),
    }
    with session_factory() as session:
        rows = session.execute(
            select(Job.source_id, Job.title, PrefilterResult.passed, PrefilterResult.reason).join(
                PrefilterResult, PrefilterResult.job_id == Job.id
            )
        ).all()
        verdicts = {(source, title): (passed, reason) for source, title, passed, reason in rows}
        for key, (passed, reason_start) in expected.items():
            assert verdicts[key][0] is passed, (key, verdicts[key])
            assert verdicts[key][1].startswith(reason_start), (key, verdicts[key])

        hn = {
            title.split(" |")[0]: passed
            for (source, title), (passed, _) in verdicts.items()
            if source == "hn_whoishiring"
        }
        assert hn == {
            "Quetzal Labs": True,
            "Lumen Ledger (Remote, LATAM) — Senior Backend Engineer — Python / Postgres": True,
            "Brightpath": False,
            "Cobalt Systems (raised $20M Series A)": True,
        }

        # Harbor & Co is on Remote OK, We Work Remotely and Real Work From Anywhere: one verdict.
        harbor = session.scalars(select(Job).where(Job.company == "Harbor & Co")).all()
        assert len(harbor) == 3 and sum(j.duplicate_of is None for j in harbor) == 1
        report = {r.source_id: r for r in source_report(session)}
        assert report["weworkremotely"].duplicates >= 1
        assert sum(r.evaluated for r in report.values()) == len(rows)
        assert dict(drop_reasons(session))["title: matches no include pattern"] >= 1
