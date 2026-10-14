"""Each Tier 1 mapper against responses recorded from a live fetch (tests/fixtures/http)."""

from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest

from job_scout.ats.platforms import Board, api_url
from job_scout.config import AppConfig
from job_scout.fetch.base import FetchContext
from job_scout.fetch.http import FetchError, HttpClient, RequestBudget
from job_scout.fetch.models import Posting
from job_scout.fetch.sources import FETCHERS

NOW = datetime(2026, 10, 12, 18, 0, tzinfo=UTC)

# The company boards in tests/fixtures/http/remoteintech (one per supported ATS).
FIXTURE_BOARDS = {
    "quetzal-labs": ("Quetzal Labs", "greenhouse", "quetzallabs"),
    "brightpath": ("Brightpath", "lever", "brightpath"),
    "nimbus-works": ("Nimbus Works", "ashby", "nimbusworks"),
    "tikal-soft": ("Tikal Soft", "workable", "tikalsoft"),
    "laguna-data": ("Laguna Data", "recruitee", "lagunadata"),
}


def fixture_boards(app_config: AppConfig) -> list[Board]:
    platforms = app_config.sources.ats_platforms
    return [
        Board(slug, company, ats, token, api_url(platforms[ats].api, token))
        for slug, (company, ats, token) in FIXTURE_BOARDS.items()
    ]


RECORDED = sorted(set(FETCHERS) - {"findwork"})  # findwork needs an API key to record


def fetch(
    app_config: AppConfig,
    http: HttpClient,
    source_id: str,
    *,
    environ: dict[str, str] | None = None,
    last_success_at: datetime | None = None,
) -> list[Posting]:
    source = app_config.sources.get(source_id)
    ctx = FetchContext(
        source=source,
        http=http,
        budget=RequestBudget(),
        now=NOW,
        last_success_at=last_success_at,
        environ=environ or {},
        boards=fixture_boards(app_config) if source.method == "ats_seed" else [],
    )
    with http:
        postings = FETCHERS[source_id].fetch(ctx)
    assert ctx.skipped_items == 0
    return postings


@pytest.mark.parametrize("source_id", RECORDED)
def test_every_recorded_source_maps(
    app_config: AppConfig, make_http: Callable[..., HttpClient], source_id: str
) -> None:
    postings = fetch(app_config, make_http(), source_id)
    assert postings
    for p in postings:
        assert p.title and p.canonical_url.startswith("https://")
        assert p.posted_at is not None and p.posted_at.tzinfo is not None
        assert p.raw


def by_title(postings: list[Posting], title: str) -> Posting:
    return next(p for p in postings if p.title == title)


def test_4dayweek(app_config: AppConfig, make_http: Callable[..., HttpClient]) -> None:
    postings = fetch(app_config, make_http(), "4dayweek")
    assert [p.source_job_id for p in postings] == ["4dw-0001", "4dw-0002", "4dw-0003", "4dw-0004"]
    p = postings[0]
    # The API returns cents; stored as dollars.
    assert (p.salary_min, p.salary_max, p.salary_currency, p.salary_period) == (
        140_000,
        170_000,
        "USD",
        "year",
    )
    assert {"schedule: 4_day_week", "hours/week: 32-32", "Kubernetes"} <= set(p.tags)
    assert p.remote_raw == "remote"
    tidewater = postings[1]  # remote job listing, hybrid company: policy goes to tags only
    assert tidewater.remote_raw == "remote"
    assert "company remote level: hybrid" in tidewater.tags
    assert p.location_raw == "Germany (remote); timezones: CET"
    assert postings[1].location_raw == "Austin, Texas, United States (hybrid)"
    assert postings[3].location_raw == "company hires worldwide"
    assert (postings[3].salary_max, postings[3].salary_period) == (11_000, "month")


def test_4dayweek_incremental_window(
    app_config: AppConfig, make_http: Callable[..., HttpClient]
) -> None:
    requested: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        return httpx.Response(200, json={"data": [], "has_more": False})

    http = make_http({"https://4dayweek.io/api/v2/jobs": handler})
    fetch(app_config, http, "4dayweek", last_success_at=NOW)
    # Merged into the configured query, never replacing it; at least a week of overlap.
    assert requested[0].params["posted_after"] == "7"
    assert requested[0].params["limit"] == "100"
    assert requested[0].params["category"] == "engineering"


def test_remoteok(app_config: AppConfig, make_http: Callable[..., HttpClient]) -> None:
    postings = fetch(app_config, make_http(), "remoteok")
    assert len(postings) == 3  # the first element is the legal notice
    p = postings[0]
    assert (p.company, p.title, p.location_raw) == (
        "Quetzal Labs",
        "Senior Django Developer",
        "Worldwide",
    )
    assert p.description_text.startswith("\U0001f680 About Quetzal Labs")  # mojibake repaired
    assert (p.salary_min, p.salary_max) == (130_000, 160_000)
    assert postings[1].salary_max is None  # salary 0 means unlisted


def test_remotive(app_config: AppConfig, make_http: Callable[..., HttpClient]) -> None:
    postings = fetch(app_config, make_http(), "remotive")  # first run: checks categories
    p = by_title(postings, "Senior Full Stack Engineer")
    assert (p.company, p.location_raw) == ("Cobalt Systems", "Americas")
    assert (p.salary_min, p.salary_max) == (120_000, 150_000)
    assert p.posted_at == datetime(2026, 10, 8, 21, 47, 32, tzinfo=UTC)  # naive -> UTC


def test_remotive_rejects_unknown_category(
    app_config: AppConfig, make_http: Callable[..., HttpClient]
) -> None:
    source = app_config.sources.get("remotive")
    bad = source.model_copy(
        update={"endpoints": ["https://remotive.com/api/remote-jobs?category=nope"]}
    )
    ctx = FetchContext(source=bad, http=make_http(), budget=RequestBudget(), now=NOW)
    with ctx.http, pytest.raises(FetchError, match=r"unknown Remotive categories \['nope'\]"):
        FETCHERS["remotive"].fetch(ctx)


def test_weworkremotely(app_config: AppConfig, make_http: Callable[..., HttpClient]) -> None:
    postings = fetch(app_config, make_http(), "weworkremotely")
    p = by_title(postings, "Senior Back-End Engineer")
    assert p.company == "Harbor & Co"
    assert p.location_raw.startswith("region: Anywhere in the World; countries:")
    assert "Guatemala" in p.location_raw
    # "Python, PostgreSQL, and Kubernetes": the Oxford comma doesn't leak into a tag.
    assert {"Python", "PostgreSQL", "Kubernetes", "Full-Time"} <= set(p.tags)
    assert not any(t.startswith("and ") for t in p.tags)
    cobalt = by_title(postings, "Full-Stack Developer")
    assert (cobalt.salary_min, cobalt.salary_max) == (100_000, 115_000)  # labelled salary line


def test_title_at_company_feeds(
    app_config: AppConfig, make_http: Callable[..., HttpClient]
) -> None:
    p = fetch(app_config, make_http(), "typescriptjobs")[1]
    assert (p.title, p.company) == ("Mid-Level Product Engineer", "Lattice Forge Inc.")
    p = fetch(app_config, make_http(), "realworkfromanywhere")[0]
    assert (p.title, p.company) == ("Python Software Engineer (remote work)", "Lattice Forge")


def test_malformed_feed_still_yields_entries(
    app_config: AppConfig, make_http: Callable[..., HttpClient]
) -> None:
    postings = fetch(app_config, make_http(), "realworkfromanywhere")
    assert by_title(postings, "Platform Engineer").description_text == "R&D platform team"


def test_remotefirstjobs_salary_typo(
    app_config: AppConfig, make_http: Callable[..., HttpClient]
) -> None:
    postings = fetch(app_config, make_http(), "remotefirstjobs")
    devops = by_title(postings, "Senior DevOps Engineer")
    assert devops.salary_max == 115_000  # "$95,00" typo line not read as $95/year
    claims = by_title(postings, "Claims Representative")
    assert (claims.salary_min, claims.salary_max) == (74_200, 92_700)


def test_pyjobs(app_config: AppConfig, make_http: Callable[..., HttpClient]) -> None:
    postings = fetch(app_config, make_http(), "pyjobs")
    p = postings[0]
    assert (p.title, p.company, p.location_raw, p.remote_raw) == (
        "Senior Python Engineer",
        "Quetzal Labs",
        None,
        "Geo remote",
    )
    assert (p.salary_min, p.salary_max, p.salary_period) == (140_000, 170_000, "year")
    intern = postings[1]
    assert (intern.company, intern.location_raw, intern.remote_raw, intern.tags) == (
        "Brightpath GmbH",
        "Berlin",
        "on-site (Office)",
        ["Internship"],
    )
    assert (intern.salary_max, intern.salary_currency, intern.salary_period) == (
        1_500,
        "EUR",
        "month",
    )
    assert postings[2].title == "Backend Engineer (R&D)"  # &amp; inside CDATA unescaped


def test_remotegamejobs(app_config: AppConfig, make_http: Callable[..., HttpClient]) -> None:
    p = fetch(app_config, make_http(), "remotegamejobs")[0]
    assert (p.company, p.title) == (
        "Pixel Harbor Studios",
        "Senior Backend Engineer - Online Services",
    )


def test_findjobit(app_config: AppConfig, make_http: Callable[..., HttpClient]) -> None:
    p = fetch(app_config, make_http(), "findjobit")[0]
    assert (p.company, p.title) == ("Quetzal Labs", "Senior Backend Engineer")
    assert p.location_raw == "Guatemala,México,Colombia"
    assert p.tags == ["Ingeniero Backend", "Tiempo Completo"]
    assert fetch(app_config, make_http(), "findjobit")[1].title == "Diseñadora UX"  # "busca una"


def test_hn(app_config: AppConfig, make_http: Callable[..., HttpClient]) -> None:
    postings = fetch(app_config, make_http(), "hn_whoishiring")
    # Deleted comments and replies are not postings; "Who wants to be hired?" is skipped.
    assert [p.source_job_id for p in postings] == ["49000010", "49000011", "49000013", "49000014"]
    pipe = postings[0]
    assert pipe.company == "Quetzal Labs"
    assert pipe.canonical_url == "https://news.ycombinator.com/item?id=49000010"
    assert pipe.title.startswith("Quetzal Labs | Senior Platform Engineer | REMOTE (Americas)")
    assert (pipe.salary_min, pipe.salary_max) == (150_000, 190_000)
    em_dash = postings[1]
    assert em_dash.company == "Lumen Ledger (Remote, LATAM)"
    assert "\n" not in em_dash.title and "Payments" not in em_dash.title
    funded = postings[3]
    assert (funded.salary_min, funded.salary_max) == (140_000, 170_000)  # not the $20M round


def test_working_nomads_keeps_development_only(
    app_config: AppConfig, make_http: Callable[..., HttpClient]
) -> None:
    postings = fetch(app_config, make_http(), "workingnomads")
    assert {p.raw["category_name"] for p in postings} == {"Development"}


def test_findwork_sends_token_and_follows_pages(
    app_config: AppConfig, make_http: Callable[..., HttpClient]
) -> None:
    """Shape from the Findwork docs; unverified against the live API until a key exists."""
    seen_auth: list[str | None] = []
    item = {
        "id": 7,
        "role": "Senior Python Engineer",
        "company_name": "Acme",
        "location": "Remote",
        "remote": True,
        "text": "<p>Build things</p>",
        "url": "https://findwork.dev/7/senior-python-engineer-at-acme",
        "date_posted": "2026-10-11T10:00:00Z",
        "keywords": ["python", "django"],
        "employment_type": "full time",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers.get("authorization"))
        if "page=2" in str(request.url):
            return httpx.Response(200, json={"next": None, "results": [{**item, "id": 8}]})
        return httpx.Response(
            200, json={"next": "https://findwork.dev/api/jobs/?page=2", "results": [item]}
        )

    http = make_http({"https://findwork.dev/api/jobs/": handler})
    postings = fetch(app_config, http, "findwork", environ={"FINDWORK_API_KEY": "secret"})
    assert [p.source_job_id for p in postings] == ["7", "8"]
    assert seen_auth == ["Token secret", "Token secret"]
    assert postings[0].remote_raw == "remote"
    assert postings[0].description_text == "Build things"
