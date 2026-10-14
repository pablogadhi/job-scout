"""ATS seed, board detection, board polling and `ats detect` (synthetic data, no network)."""

import csv
import io
import tarfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from job_scout.ats.detect import (
    Detection,
    Detector,
    board_from_url,
    find_board,
    slug_candidates,
)
from job_scout.ats.platforms import api_url
from job_scout.ats.refresh import refresh
from job_scout.ats.seed import SeedCompany, keep, parse_seed
from job_scout.ats.store import apply_watchlist, boards, save_detection, upsert_seed
from job_scout.cli import app
from job_scout.config import AppConfig, ConfigError, load_config
from job_scout.db import make_engine, make_session_factory
from job_scout.db.models import AtsCompany, AtsMethod, AtsSeed, AtsStatus, Job
from job_scout.fetch.http import HttpClient
from job_scout.fetch.runner import run_fetch
from tests.conftest import HTTP_FIXTURES, REPO_CONFIG, edit
from tests.test_fetchers import FIXTURE_BOARDS

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
SEED = (HTTP_FIXTURES / "remoteintech" / "01.tar.gz").read_bytes()
SEED_URL = "https://api.github.com/repos/remoteintech/remote-jobs/tarball"

Route = Callable[[httpx.Request], httpx.Response]


def web(pages: dict[str, httpx.Response | Route]) -> httpx.MockTransport:
    """A scripted web: exact URLs (query included) answer as given; robots.txt is absent;
    any other URL is a 404, which is what an unknown ATS board returns."""

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in pages:
            page = pages[url]
            return page(request) if callable(page) else page
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(404, text="Not Found")

    return httpx.MockTransport(handle)


def html(body: str) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/html"}, text=f"<html>{body}</html>")


def jobs(n: int = 1) -> httpx.Response:
    return httpx.Response(200, json={"jobs": [{"id": i} for i in range(n)]})


def company(slug: str, **fields: str | None) -> SeedCompany:
    defaults: dict[str, str | None] = {
        "name": slug.title(),
        "website": None,
        "careers_url": None,
        "region": "worldwide",
        "remote_policy": "fully-remote",
    }
    return SeedCompany(slug=slug, **{**defaults, **fields})  # type: ignore[arg-type]


def detector(app_config: AppConfig, pages: dict[str, httpx.Response | Route]) -> Detector:
    http = HttpClient(app_config.sources.defaults, transport=web(pages), sleep=lambda _s: None)
    http.__enter__()
    return Detector(http, app_config.sources.get("remoteintech"), app_config.sources.ats_platforms)


# --- seed ----------------------------------------------------------------------------------


def test_parse_seed_and_filter(app_config: AppConfig) -> None:
    companies = parse_seed(SEED)
    assert len(companies) == 7  # broken.md (no frontmatter) and README.md skipped
    quetzal = next(c for c in companies if c.slug == "quetzal-labs")
    assert quetzal == SeedCompany(
        slug="quetzal-labs",
        name="Quetzal Labs",
        website="https://quetzal-labs.example/",
        careers_url="https://boards.greenhouse.io/quetzallabs",
        region="worldwide",
        remote_policy="fully-remote",
    )
    source = app_config.sources.get("remoteintech")
    kept = keep(companies, source.seed_regions, source.seed_remote_policies)
    assert sorted(c.slug for c in kept) == sorted(FIXTURE_BOARDS)  # europe + hybrid dropped


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("https://acme.example/careers/,", "https://acme.example/careers/"),
        (
            "https://acme.notion.example/career](https://acme.notion.example/Work-with-us)",
            "https://acme.notion.example/career",
        ),
        ("https://a.example/jobs, https://b.example/jobs", "https://a.example/jobs"),
        ("https://acme.example/careers#open", "https://acme.example/careers#open"),
    ],
)
def test_seed_urls_are_cleaned(raw: str, clean: str) -> None:
    from job_scout.ats.seed import parse_company

    text = f"---\ntitle: Acme\nslug: acme\ncareers_url: {raw}\nregion: worldwide\n---\n"
    parsed = parse_company("acme.md", text)
    assert parsed is not None and parsed.careers_url == clean


def test_malformed_seed_file_is_skipped(caplog: pytest.LogCaptureFixture) -> None:
    files = {
        "good.md": "---\ntitle: Good Co\nslug: Good-Co\ncareers_url: https://good.example/jobs\n---\n",
        "blank-title.md": "---\ntitle: '  '\nregion: worldwide\n---\n",
        "listed.md": "---\ntitle: Listed\ncareers_url: [https://a.example, https://b.example]\n---\n",
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"remote-jobs-abc/src/companies/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    companies = {c.slug: c for c in parse_seed(buffer.getvalue())}
    assert set(companies) == {"good-co", "blank-title"}
    assert companies["blank-title"].name == "blank-title"  # falls back to the slug
    assert "listed.md" in caplog.text  # a list where a URL belongs: logged and skipped


# --- detection -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "board"),
    [
        ("https://boards.greenhouse.io/acme", ("greenhouse", "acme")),
        ("https://boards.greenhouse.io/acme/jobs/123", ("greenhouse", "acme")),
        ("https://job-boards.greenhouse.io/embed/job_board/js?for=acme", ("greenhouse", "acme")),
        ("https://jobs.lever.co/acme-2", ("lever", "acme-2")),
        ("https://jobs.ashbyhq.com/Acme/3cd3f073", ("ashby", "Acme")),
        ("https://apply.workable.com/acme/?lng=en#jobs", ("workable", "acme")),
        ("https://acme.workable.com/", ("workable", "acme")),
        ("https://acme.recruitee.com/", ("recruitee", "acme")),
        ("https://careers.smartrecruiters.com/Acme", None),  # not polled: robots.txt
        ("https://acme.example/careers", None),
        (None, None),
    ],
)
def test_board_from_url(app_config: AppConfig, url: str | None, board: tuple | None) -> None:
    assert board_from_url(url, app_config.sources.ats_platforms) == board


def test_find_board_in_page_skips_non_tokens(app_config: AppConfig) -> None:
    page = '<a href="https://apply.workable.com/j/AB12CD">job</a> <a href="https://apply.workable.com/acme/">all</a>'
    assert find_board(page, app_config.sources.ats_platforms) == ("workable", "acme")


def test_slug_candidates() -> None:
    c = company("tikal-soft", website="https://www.tikalsoft.example/")
    assert slug_candidates(c) == ["tikal-soft", "tikalsoft"]


def test_detect_careers_url_needs_no_request(app_config: AppConfig) -> None:
    d = detector(app_config, {})
    result = d.detect(company("acme", careers_url="https://jobs.lever.co/acme"))
    assert (result.ats, result.token, result.method) == ("lever", "acme", AtsMethod.CAREERS_URL)
    assert d.budget.used == 0


def test_detect_from_careers_page_embed(app_config: AppConfig) -> None:
    page = html('<script src="https://jobs.ashbyhq.com/nimbusworks/embed?version=2"></script>')
    d = detector(app_config, {"https://nimbusworks.example/careers": page})
    result = d.detect(company("nimbus-works", careers_url="https://nimbusworks.example/careers"))
    assert (result.ats, result.token, result.method) == (
        "ashby",
        "nimbusworks",
        AtsMethod.CAREERS_PAGE,
    )


def test_detect_follows_the_website_careers_link(app_config: AppConfig) -> None:
    d = detector(
        app_config,
        {
            "https://www.tikalsoft.example/": html('<a href="/join-us">Careers</a>'),
            "https://www.tikalsoft.example/join-us": html(
                '<a href="https://apply.workable.com/tikalsoft/">Open roles</a>'
            ),
        },
    )
    result = d.detect(company("tikal-soft", website="https://www.tikalsoft.example/"))
    assert (result.ats, result.token, result.method) == (
        "workable",
        "tikalsoft",
        AtsMethod.CAREERS_PAGE,
    )


def test_detect_by_probing_slugs(app_config: AppConfig) -> None:
    platforms = app_config.sources.ats_platforms
    d = detector(
        app_config,
        {
            "https://orbitops.example/": html("<p>No links here.</p>"),
            # The slug answers on Lever with no jobs: not a match. The domain label has jobs.
            api_url(platforms["lever"].api, "orbit-ops"): httpx.Response(200, json=[]),
            api_url(platforms["greenhouse"].api, "orbitops"): jobs(2),
        },
    )
    result = d.detect(company("orbit-ops", website="https://orbitops.example/"))
    assert (result.ats, result.token, result.method) == ("greenhouse", "orbitops", AtsMethod.PROBE)


def test_detect_unknown_and_robots_blocked_platform(app_config: AppConfig) -> None:
    d = detector(
        app_config,
        {
            "https://api.ashbyhq.com/robots.txt": httpx.Response(
                200, text="User-agent: *\nDisallow: /\n"
            ),
            "https://ghost.example/careers": httpx.Response(500),
        },
    )
    result = d.detect(company("ghost", careers_url="https://ghost.example/careers"))
    assert result.ats is None and result.method is None
    assert "HTTP 500" in (result.note or "") and "ashby probing off" in (result.note or "")
    assert result.retry  # a server error is temporary: detect again later

    d = detector(app_config, {"https://gone.example/careers": httpx.Response(410)})
    result = d.detect(company("gone", careers_url="https://gone.example/careers"))
    assert result.ats is None and not result.retry  # nothing temporary failed: unknown


def test_rate_limited_probe_is_inconclusive(app_config: AppConfig) -> None:
    workable = api_url(app_config.sources.ats_platforms["workable"].api, "busyco")
    d = detector(app_config, {workable: httpx.Response(429)})
    result = d.detect(company("busyco"))
    assert result.ats is None and result.retry
    assert "workable probe: HTTP 429" in (result.note or "")
    # The same detector, next company: the flag doesn't carry over.
    assert not d.detect(company("quietco")).retry


def test_robots_401_does_not_block_probing(app_config: AppConfig) -> None:
    """Ashby's API answers robots.txt with 401: unavailable, so allowed (RFC 9309)."""
    d = detector(
        app_config,
        {
            "https://api.ashbyhq.com/robots.txt": httpx.Response(401),
            api_url(app_config.sources.ats_platforms["ashby"].api, "skyline"): jobs(1),
        },
    )
    result = d.detect(company("skyline"))
    assert (result.ats, result.token, result.method) == ("ashby", "skyline", AtsMethod.PROBE)


# --- store + refresh -----------------------------------------------------------------------


def refresh_web(app_config: AppConfig) -> HttpClient:
    """Seed tarball plus what detection needs for the five kept fixture companies."""
    pages: dict[str, httpx.Response | Route] = {
        SEED_URL: httpx.Response(200, content=SEED),
        "https://nimbusworks.example/careers": html(
            '<a href="https://jobs.ashbyhq.com/nimbusworks">Jobs</a>'
        ),
        "https://www.tikalsoft.example/": html('<a href="/careers">Careers</a>'),
        "https://www.tikalsoft.example/careers": html("<p>Email us.</p>"),
        api_url(app_config.sources.ats_platforms["workable"].api, "tikalsoft"): httpx.Response(
            200, json={"jobs": [{"shortcode": "X"}]}
        ),
    }
    http = HttpClient(app_config.sources.defaults, transport=web(pages), sleep=lambda _s: None)
    http.__enter__()
    return http


def rows(sf: sessionmaker[Session]) -> dict[str, AtsCompany]:
    with sf() as session:
        return {r.slug: r for r in session.scalars(select(AtsCompany))}


def test_refresh_detects_every_fixture_company(
    app_config: AppConfig, session_factory: sessionmaker[Session]
) -> None:
    report = refresh(app_config, session_factory, refresh_web(app_config), now=NOW)
    assert report.seed_downloaded and (report.seed_total, report.seed_kept) == (7, 5)
    assert report.changes.added == 5 and report.remaining == 0
    found = rows(session_factory)
    expected = {slug: (ats, token) for slug, (_, ats, token) in FIXTURE_BOARDS.items()}
    assert {s: (r.ats, r.token) for s, r in found.items() if s in expected} == expected
    assert found["tikal-soft"].method == AtsMethod.PROBE
    assert found["nimbus-works"].method == AtsMethod.CAREERS_PAGE
    # The watchlist (Canonical) is polled whatever the seed says.
    canonical = found["canonical"]
    assert (canonical.ats, canonical.token, canonical.source) == (
        "greenhouse",
        "canonical",
        AtsSeed.WATCHLIST,
    )
    assert all(r.status == AtsStatus.OK for r in found.values())

    # Within the refresh interval nothing is downloaded or re-detected.
    again = refresh(
        app_config, session_factory, refresh_web(app_config), now=NOW + timedelta(days=1)
    )
    assert not again.seed_downloaded and sum(again.detected.values()) == 0


def test_seed_changes_and_watchlist_removal(
    app_config: AppConfig, session_factory: sessionmaker[Session]
) -> None:
    acme = company("acme", careers_url="https://jobs.lever.co/acme")
    with session_factory.begin() as session:
        upsert_seed(session, [acme, company("beta")], NOW)
    with session_factory.begin() as session:
        changes = upsert_seed(
            session,
            [company("acme", name="Acme", careers_url="https://acme.example/jobs")],
            NOW,
        )
    assert (changes.added, changes.updated, changes.dropped) == (0, 1, 1)
    found = rows(session_factory)
    assert found["beta"].status == AtsStatus.NOT_IN_SEED
    assert found["acme"].detected_at is None  # careers URL changed: detect again

    with session_factory.begin() as session:
        apply_watchlist(session, app_config.sources.watchlist, NOW)
        apply_watchlist(session, [], NOW)  # Canonical removed from the watchlist
    assert rows(session_factory)["canonical"].status == AtsStatus.NOT_IN_SEED


def test_rate_limited_detection_is_retried_then_given_up(
    app_config: AppConfig, session_factory: sessionmaker[Session]
) -> None:
    """Workable rate-limits the probe that would find Tikal Soft: it stays pending (detected
    again next run) and only counts as unknown after three inconclusive tries."""
    workable = api_url(app_config.sources.ats_platforms["workable"].api, "tikalsoft")
    pages: dict[str, httpx.Response | Route] = {
        SEED_URL: httpx.Response(200, content=SEED),
        "https://nimbusworks.example/careers": html(
            '<a href="https://jobs.ashbyhq.com/nimbusworks">Jobs</a>'
        ),
        workable: httpx.Response(429),
    }
    http = HttpClient(app_config.sources.defaults, transport=web(pages), sleep=lambda _s: None)
    http.__enter__()

    report = refresh(app_config, session_factory, http, now=NOW)
    assert report.detected["retry later"] == 1 and report.remaining == 1
    tikal = rows(session_factory)["tikal-soft"]
    assert (tikal.status, tikal.detected_at, tikal.detection_retries) == (
        AtsStatus.PENDING,
        None,
        1,
    )
    assert "attempt 1 of 3" in (tikal.last_poll_error or "")

    for day in (1, 2):
        report = refresh(app_config, session_factory, http, now=NOW + timedelta(days=day))
        assert sum(report.detected.values()) == 1  # only Tikal Soft is detected again
    tikal = rows(session_factory)["tikal-soft"]
    assert tikal.status == AtsStatus.UNKNOWN_ATS and tikal.detection_retries == 0
    assert "gave up after 3 inconclusive detections" in (tikal.last_poll_error or "")
    assert report.detected["unknown"] == 1 and report.remaining == 0


def test_inconclusive_redetection_keeps_a_working_board() -> None:
    row = AtsCompany(
        slug="acme",
        company="Acme",
        ats="lever",
        token="acme",
        method=AtsMethod.CAREERS_URL,
        status=AtsStatus.OK,
        source=AtsSeed.REMOTEINTECH,
        detection_retries=0,
    )
    retry = Detection(note="lever probe: HTTP 503", retry=True)
    assert [save_detection(row, retry, NOW) for _ in range(3)] == [
        "retry later",
        "retry later",
        "lever",
    ]
    assert (row.status, row.ats, row.detected_at, row.detection_retries) == (
        AtsStatus.OK,
        "lever",
        NOW,
        0,
    )


# --- polling -------------------------------------------------------------------------------


def add_fixture_boards(sf: sessionmaker[Session]) -> None:
    with sf.begin() as session:
        for slug, (name, ats, token) in FIXTURE_BOARDS.items():
            session.add(
                AtsCompany(
                    slug=slug,
                    company=name,
                    ats=ats,
                    token=token,
                    status=AtsStatus.OK,
                    source=AtsSeed.REMOTEINTECH,
                    method=AtsMethod.CAREERS_URL,
                )
            )


def test_poll_boards_and_store(
    app_config: AppConfig,
    session_factory: sessionmaker[Session],
    make_http: Callable[..., HttpClient],
) -> None:
    add_fixture_boards(session_factory)
    with session_factory() as session:
        assert len(boards(session, app_config.sources.ats_platforms)) == 5
    laguna = api_url(app_config.sources.ats_platforms["recruitee"].api, "lagunadata")
    with make_http({laguna: lambda r: httpx.Response(500)}) as http:
        (outcome,) = run_fetch(app_config, session_factory, http, source_ids=["remoteintech"])
    assert outcome.status == "ok" and outcome.reason == "1 of 5 company boards failed"
    assert outcome.new == 6  # greenhouse 2, lever 2, ashby 1 (one unlisted), workable 1
    found = rows(session_factory)
    assert (
        found["laguna-data"].last_poll_error and "HTTP 500" in found["laguna-data"].last_poll_error
    )
    assert (
        found["quetzal-labs"].last_polled_at is not None
        and found["quetzal-labs"].last_poll_error is None
    )

    with session_factory() as session:
        jobs_by_title = {j.title: j for j in session.scalars(select(Job))}
    backend = jobs_by_title["Senior Backend Engineer (Python)"]
    assert (backend.source_id, backend.company) == ("remoteintech", "Quetzal Labs")
    assert backend.source_job_id == "greenhouse:quetzallabs:4000001"
    assert (backend.salary_min, backend.salary_max, backend.salary_currency) == (
        140_000,
        170_000,
        "USD",
    )
    assert "&lt;" not in (backend.description_text or "") and "Django" in backend.description_text
    platform = jobs_by_title["Senior Platform Engineer"]
    assert (platform.remote_raw, platform.salary_max, platform.salary_period) == (
        "remote",
        160_000,
        "year",
    )
    assert platform.location_raw == "Remote - LATAM; Remote - US"
    data = jobs_by_title["Staff Software Engineer, Data"]
    assert (data.salary_min, data.remote_raw) == (
        125_000,
        "Remote",
    ) and "Mexico" in data.location_raw
    assert "Unlisted Role" not in jobs_by_title


def test_recruitee_and_workable_mapping(
    app_config: AppConfig,
    session_factory: sessionmaker[Session],
    make_http: Callable[..., HttpClient],
) -> None:
    add_fixture_boards(session_factory)
    with make_http() as http:
        run_fetch(app_config, session_factory, http, source_ids=["remoteintech"])
    with session_factory() as session:
        found = {j.title: j for j in session.scalars(select(Job))}
    devops = found["Senior DevOps Engineer"]
    assert (devops.remote_raw, devops.salary_max, devops.salary_period) == (
        "remote",
        11_000,
        "month",
    )
    assert devops.posted_at == datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    assert "Terraform" in devops.description_text
    fullstack = found["Senior Full-Stack Developer"]
    assert (
        fullstack.remote_raw == "remote"
        and fullstack.location_raw == "Guatemala City, Guatemala, Guatemala"
    )


# --- config + CLI --------------------------------------------------------------------------


def test_unsupported_ats_platform_is_a_config_error(config_dir: Path) -> None:
    edit(
        config_dir / "sources.yaml",
        "  recruitee:",
        '  bamboohr: {detect: "bamboohr.com", api: "https://{company}.bamboohr.com/list"}\n'
        "  recruitee:",
    )
    with pytest.raises(ConfigError, match="no detector or fetcher for bamboohr"):
        load_config(config_dir)


def test_ats_detect_command(
    app_config: AppConfig, db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pages = {SEED_URL: httpx.Response(200, content=SEED)}  # nothing else answers: unknown ATS

    def client(defaults: object, **kwargs: object) -> HttpClient:
        return HttpClient(app_config.sources.defaults, transport=web(pages), sleep=lambda _s: None)

    monkeypatch.setattr("job_scout.cli.ats.HttpClient", client)
    monkeypatch.setenv("JOB_SCOUT_PATHS__REPORTS", str(tmp_path / "reports"))
    result = CliRunner().invoke(
        app, ["--config-dir", str(REPO_CONFIG), "ats", "detect"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.output
    assert "seed: 7 companies, 5 kept by the filters (5 new" in result.output
    assert "Company boards" in result.output and "Unknown ATS (2)" in result.output
    # careers_url hits need no page; nimbus-works and tikal-soft have no pages here: unknown.
    with make_session_factory(make_engine(db_url))() as session:
        status = {r.slug: (r.status, r.ats) for r in session.scalars(select(AtsCompany))}
    assert status["quetzal-labs"] == (AtsStatus.OK, "greenhouse")
    assert status["canonical"] == (AtsStatus.OK, "greenhouse")  # watchlist
    assert status["nimbus-works"] == (AtsStatus.UNKNOWN_ATS, None)
    (report,) = (tmp_path / "reports").glob("ats-unknown-*.csv")
    names = [row["company"] for row in csv.DictReader(report.open())]
    assert names == ["Nimbus Works", "Tikal Soft"]
