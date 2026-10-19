"""Each Tier 1 mapper against responses recorded from a live fetch (tests/fixtures/http)."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from job_scout.ats.platforms import Board, api_url
from job_scout.cli.sources import with_max_pages
from job_scout.config import AppConfig
from job_scout.config.sources import BOARD_METHODS
from job_scout.fetch.base import FetchContext
from job_scout.fetch.hn import map_comment
from job_scout.fetch.http import HttpClient, RequestBudget
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


# The Workday board in tests/fixtures/http/company_boards (POSTed pages, a GET per job).
WORKDAY_BOARD = (
    "selva-aid",
    "Selva Aid",
    "workday",
    "selvaaid.wd5.myworkdayjobs.com/selva_aid/Careers",
)


def workday_board(app_config: AppConfig) -> Board:
    slug, company, ats, token = WORKDAY_BOARD
    return Board(
        slug, company, ats, token, api_url(app_config.sources.ats_platforms[ats].api, token)
    )


def fixture_boards(app_config: AppConfig) -> list[Board]:
    platforms = app_config.sources.ats_platforms
    return [
        *(
            Board(slug, company, ats, token, api_url(platforms[ats].api, token))
            for slug, (company, ats, token) in FIXTURE_BOARDS.items()
        ),
        workday_board(app_config),
    ]


RECORDED = sorted(set(FETCHERS) - {"findwork"})  # findwork needs an API key to record


def fetch(
    app_config: AppConfig,
    http: HttpClient,
    source_id: str,
    *,
    environ: dict[str, str] | None = None,
    last_success_at: datetime | None = None,
    known_ids: frozenset[str] = frozenset(),
) -> list[Posting]:
    source = app_config.sources.get(source_id)
    ctx = FetchContext(
        source=source,
        http=http,
        budget=RequestBudget(),
        now=NOW,
        last_success_at=last_success_at,
        environ=environ or {},
        boards=fixture_boards(app_config) if source.method in BOARD_METHODS else [],
        known_ids=known_ids,
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
    # A remote location with a country is where applicants must live ("Must be based in").
    assert p.location_raw == "remote, must be based in Germany; timezones: CET"
    assert postings[1].location_raw == "Austin, Texas, United States (hybrid)"
    assert postings[3].location_raw == "company hires worldwide"
    assert (postings[3].salary_max, postings[3].salary_period) == (11_000, "month")


def test_4dayweek_remote_countries_are_a_residency_rule() -> None:
    from job_scout.fetch.sources import _fourdayweek_location

    def loc(city: str | None, country: str | None, continent: str, arrangement: str) -> dict:
        return {
            "city": city,
            "country": country,
            "continent": continent,
            "work_arrangement": arrangement,
        }

    item = {
        "locations": [
            loc("Austin", "United States", "North America", "hybrid"),
            loc("New York", "United States", "North America", "remote"),
            loc(None, "United States", "North America", "remote"),
            loc(None, "Canada", "North America", "remote"),
            loc(None, None, "South America", "remote"),
        ]
    }
    assert _fourdayweek_location(item) == (
        "remote, must be based in United States or Canada; "
        "Austin, United States (hybrid); South America (remote)"
    )


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
    assert (p.description_text or "").startswith(
        "\U0001f680 About Quetzal Labs"
    )  # mojibake repaired
    assert (p.salary_min, p.salary_max) == (130_000, 160_000)
    assert postings[1].salary_max is None  # salary 0 means unlisted


def test_remotive(app_config: AppConfig, make_http: Callable[..., HttpClient]) -> None:
    postings = fetch(app_config, make_http(), "remotive")
    p = by_title(postings, "Senior Full Stack Engineer")
    assert p.source_job_id == "7000001"  # jobId: the JSON API's id, so stored jobs still match
    assert (p.company, p.location_raw) == ("Cobalt Systems", "Americas")
    assert {"Software Development", "full_time"} <= set(p.tags)
    assert (p.salary_min, p.salary_max) == (120_000, 150_000)  # from the description
    assert p.posted_at == datetime(2026, 10, 8, 21, 47, 32, tzinfo=UTC)
    assert by_title(postings, "Inside Sales Representative").salary_max is None


def test_reliefweb(app_config: AppConfig, make_http: Callable[..., HttpClient]) -> None:
    postings = fetch(app_config, make_http(), "reliefweb")
    p = by_title(postings, "Programme Manager, Food Security")
    assert p.source_job_id == "8100001"  # the API's id
    assert (p.company, p.location_raw) == ("Ceiba Relief & Development", "Côte d'Ivoire")
    assert {"Program/Project Management", "Job"} <= set(p.tags)
    assert (p.description_text or "").startswith("Country: Côte d'Ivoire")
    assert p.posted_at == datetime(2026, 10, 15, 13, 27, 17, tzinfo=UTC)
    home = by_title(postings, "Senior Grants Manager (home-based)")
    assert home.location_raw is None  # no country: the site's Remote / Roster / Roving list
    assert (home.salary_min, home.salary_max) == (70_000, 82_000)


def test_unvacancies(app_config: AppConfig, make_http: Callable[..., HttpClient]) -> None:
    postings = fetch(app_config, make_http(), "unvacancies")
    p = by_title(postings, "Senior Programme Consultant, Water & Sanitation")
    url = "https://unvacancies.org/jobs/senior-programme-consultant-water-sanitation-QZ-51003"
    assert (p.source_job_id, p.canonical_url) == (url, url)  # without the tracking parameters
    assert p.company == "UNICEF"
    assert p.posted_at == datetime(2026, 10, 15, tzinfo=UTC)
    # The feed has one line; the page has the place, who can apply and the notice.
    assert p.location_raw == (
        "Remote (linked to Panama City, Panama); who can apply: Nationals of Panama only; "
        "National hire"
    )
    assert p.remote_raw == "Fully Remote"
    assert p.description_text == (
        "Who can apply: Nationals of Panama only; National hire\n"
        "Contract: Consultant · 11 months\n"
        "Required languages: Spanish and English\n\n"
        "Coordinate WASH programmes.\n\nTwo missions."
    )
    assert p.raw["page"]["details"] == {"Work mode": "Fully Remote"}
    # A job under two functions comes in both feeds: its page is read once, and the store
    # keeps one copy.
    shared = [q for q in postings if q.title == "Programme and Evaluation Specialist"]
    assert len(shared) == 2 and shared[0] == shared[1]
    assert shared[0].location_raw == "Home-based"
    assert shared[0].description_text == (
        "Contract: Fixed term · P-4\n\n"
        "UNFPA seeks a home-based specialist to plan and evaluate country programmes."
    )


def test_unvacancies_without_pages(
    app_config: AppConfig, make_http: Callable[..., HttpClient]
) -> None:
    """A job already stored isn't read again; the feed's fields stay."""
    url = "https://unvacancies.org/jobs/senior-programme-consultant-water-sanitation-QZ-51003"
    postings = fetch(app_config, make_http(), "unvacancies", known_ids=frozenset({url}))
    p = by_title(postings, "Senior Programme Consultant, Water & Sanitation")
    assert (p.location_raw, "page" in p.raw) == (None, False)
    assert p.remote_raw == "Remote and home-based"  # from the feed's remote=true filter
    assert (p.description_text or "").startswith("UNICEF seeks a consultant")


def test_weworkremotely(app_config: AppConfig, make_http: Callable[..., HttpClient]) -> None:
    postings = fetch(app_config, make_http(), "weworkremotely")
    p = by_title(postings, "Senior Back-End Engineer")
    assert p.company == "Harbor & Co"
    assert (p.location_raw or "").startswith("region: Anywhere in the World; countries:")
    assert "Guatemala" in (p.location_raw or "")
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
    # The feed's one line, completed from the job page's JobPosting.
    assert (p.title, p.company, p.location_raw, p.remote_raw) == (
        "Senior Python Engineer",
        "Quetzal Labs",
        "must be based in Canada or Mexico",
        "Geo remote",
    )
    assert p.description_text == (
        "Build our Django APIs.\n\nRemote, but you must be in one of these countries."
    )
    assert p.raw["page"]["jobLocationType"] == "TELECOMMUTE"
    assert "description" not in p.raw["page"]
    assert (p.salary_min, p.salary_max, p.salary_period) == (140_000, 170_000, "year")
    intern = postings[1]
    assert (intern.company, intern.location_raw, intern.remote_raw, intern.tags) == (
        "Brightpath GmbH",
        "Berlin, DE",
        "on-site (Office)",
        ["Internship"],
    )
    assert (intern.salary_max, intern.salary_currency, intern.salary_period) == (
        1_500,
        "EUR",
        "month",
    )
    rd = postings[2]
    assert rd.title == "Backend Engineer (R&D)"  # &amp; inside CDATA unescaped
    # A page without a JobPosting keeps the feed's line, and is marked as read.
    assert (rd.description_text or "").startswith("Backend Engineer (R&D) at Orbit Analytics.")
    assert rd.raw["page"] == {}


def test_pyjobs_contractor_is_not_a_place() -> None:
    from job_scout.fetch.sources import _map_pyjobs

    p = _map_pyjobs(
        {
            "title": "AI Engineer",
            "link": "https://www.pyjobs.com/job/ai-engineer-Xy12",
            "summary": "AI Engineer at Arise. Geo remote · Contractor · USD 150,000 per year.",
        }
    )
    assert (p.location_raw, p.remote_raw, p.tags) == (None, "Geo remote", ["Contractor"])


def test_typescriptjobs_reads_the_posting_from_its_page(
    app_config: AppConfig, make_http: Callable[..., HttpClient]
) -> None:
    cobalt, lattice = fetch(app_config, make_http(), "typescriptjobs")
    # The page names one country as the requirement and lists the rest as job locations.
    assert (cobalt.location_raw, cobalt.remote_raw) == ("must be based in MX or CO", "remote")
    assert cobalt.description_text == (
        "Location: Remote in Mexico or Colombia\n\nNext.js and Node services."
    )
    assert (lattice.location_raw, lattice.remote_raw) == ("Lisbon, PT", None)
    assert lattice.description_text is not None
    assert "Relocation to Lisbon is required." in lattice.description_text


def test_job_pages_are_read_once_for_new_jobs_whose_title_passes(
    app_config: AppConfig, make_http: Callable[..., HttpClient]
) -> None:
    def no_page(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"page read: {request.url}")

    pages = "https://typescriptjobs.net/jobs/"
    known = frozenset(
        f"{pages}{slug}"
        for slug in (
            "senior-typescript-engineer-at-cobalt-systems",
            "mid-level-product-engineer-at-lattice-forge-inc",
        )
    )
    stored = fetch(app_config, make_http({pages: no_page}), "typescriptjobs", known_ids=known)
    assert [p.description_text for p in stored] == ["Next.js and Node.", "Product work."]

    source = app_config.sources.get("typescriptjobs")
    http = make_http({pages: no_page})
    ctx = FetchContext(
        source=source, http=http, budget=RequestBudget(), now=NOW, title_ok=lambda _t: False
    )
    with http:
        dropped = FETCHERS["typescriptjobs"].fetch(ctx)
    assert all("page" not in p.raw for p in dropped)


def test_a_job_page_that_fails_keeps_the_feed_posting(
    app_config: AppConfig, make_http: Callable[..., HttpClient]
) -> None:
    gone = {"https://www.pyjobs.com/job/senior-python": lambda _r: httpx.Response(404)}
    p = fetch(app_config, make_http(gone), "pyjobs")[0]
    assert p.location_raw is None and "page" not in p.raw
    assert (p.description_text or "").startswith("Senior Python Engineer at Quetzal Labs.")


def test_nodesk_takes_the_place_from_the_page(
    app_config: AppConfig, make_http: Callable[..., HttpClient]
) -> None:
    postings = fetch(app_config, make_http(), "nodesk")
    platform = by_title(postings, "Senior Platform Engineer")
    assert (platform.company, platform.location_raw, platform.remote_raw) == (
        "Ceiba Cloud",
        "Remote, Worldwide",
        "remote",
    )
    assert platform.description_text == "Run our Kubernetes platform.\n\nWork from anywhere."
    assert platform.tags[-2:] == ["Kubernetes", "Go"]
    # The JobPosting says "Anywhere" for a job in India; the page's location line doesn't.
    india = by_title(postings, "Senior Python Engineer - India")
    assert india.location_raw == "Remote, India"
    assert india.raw["page"]["location_line"] == ["Remote", "India"]
    assert by_title(postings, "Account Executive").location_raw == "Remote"


def test_remotejobs_lat(app_config: AppConfig, make_http: Callable[..., HttpClient]) -> None:
    postings = fetch(app_config, make_http(), "remotejobs_lat")
    # Both programming pages, then devops; the job listed in both categories once.
    assert [p.source_job_id for p in postings] == ["70001", "70002", "69990", "70003"]
    backend = postings[0]
    assert (backend.title, backend.company, backend.remote_raw) == (
        "Senior Backend Engineer (Python)",
        "Ceiba Cloud",
        "remote",
    )
    # The page's six countries are a placeholder: the card's place stays.
    assert backend.location_raw == "Remote"
    assert backend.description_text == "We build Django services.\n\nRemote."
    assert backend.tags == ["Python", "Django", "Full time"]
    # The list shows "$7000 - $9000"; the page says per month.
    assert (backend.salary_min, backend.salary_max, backend.salary_period) == (
        7000,
        9000,
        "month",
    )
    assert backend.posted_at == datetime(2026, 10, 10, 13, 29, 58, tzinfo=UTC)
    assert postings[1].location_raw == "México Guatemala Costa Rica"
    assert postings[1].salary_max is None
    staff = postings[2]
    assert (staff.salary_max, staff.salary_period) == (180_000, "year")
    # A page without a JobPosting: no salary, and the card's age dates it.
    sre = postings[3]
    assert sre.salary_max is None and sre.raw["page"] == {}
    assert sre.posted_at == NOW - timedelta(hours=5)
    assert "Contract" in sre.tags


def test_remotejobs_lat_stops_at_a_page_with_nothing_new(
    app_config: AppConfig, make_http: Callable[..., HttpClient]
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"read: {request.url}")

    known = frozenset({"70001", "70002", "70003"})
    http = make_http(
        {
            "https://remotejobs.lat/en/category/programming?page=2": refuse,
            "https://remotejobs.lat/en/job-offers/": refuse,
        }
    )
    postings = fetch(app_config, http, "remotejobs_lat", known_ids=known)
    assert [p.source_job_id for p in postings] == ["70001", "70002", "70003"]
    # Stored jobs keep the card's fields; their pages were read when they were new.
    assert all("page" not in p.raw for p in postings)
    assert postings[0].posted_at == NOW - timedelta(days=2)


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
    # Open to one other country only, yet remote for all of LATAM: Claude reads the description.
    assert fetch(app_config, make_http(), "findjobit")[2].location_raw == "Chile"


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
    # Duplicates are matched on the role alone; the title keeps the whole header.
    roles = ["Senior Platform Engineer", "Senior Backend Engineer", "Staff Engineer"]
    assert [p.dedupe_title for p in postings] == [*roles, "Backend Engineer"]


@pytest.mark.parametrize(
    ("header", "role"),
    [
        ("Acme | Remote | Full-Time | Senior SRE | $200k", "Senior SRE"),
        ("Acme | https://a.example | a.io | Data Engineer (Remote)", "Data Engineer (Remote)"),
        ("Acme | REMOTE (US) | Full time | https://acme.example", None),
        ("Acme is hiring engineers", None),
    ],
)  # fmt: skip
def test_hn_role_for_matching_duplicates(header: str, role: str | None) -> None:
    comment = {"id": 1, "author": "a", "text": f"{header}<p>About us.</p>", "created_at": None}
    posting = map_comment(comment)
    assert posting is not None
    assert (posting.title, posting.dedupe_title) == (header, role)


def test_findwork_stops_at_max_pages(
    app_config: AppConfig, make_http: Callable[..., HttpClient]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        item = {"id": page, "role": "Backend Engineer", "url": f"https://findwork.dev/{page}"}
        next_url = f"https://findwork.dev/api/jobs/?page={page + 1}"
        return httpx.Response(200, json={"next": next_url, "results": [item]})

    http = make_http({"https://findwork.dev/api/jobs/": handler})
    config = with_max_pages(app_config, 3)
    postings = fetch(config, http, "findwork", environ={"FINDWORK_API_KEY": "secret"})
    assert [p.source_job_id for p in postings] == ["1", "2", "3"]


def test_working_nomads_keeps_development_only(
    app_config: AppConfig, make_http: Callable[..., HttpClient]
) -> None:
    postings = fetch(app_config, make_http(), "workingnomads")
    assert {p.raw["category_name"] for p in postings} == {"Development"}


@pytest.mark.parametrize(
    ("categories", "kept"),
    [
        (["Customer Success"], {"Customer Success"}),
        ([], {"Development", "Customer Success"}),  # empty keeps every category
    ],
)
def test_working_nomads_categories_come_from_config(
    app_config: AppConfig,
    make_http: Callable[..., HttpClient],
    categories: list[str],
    kept: set[str],
) -> None:
    source = app_config.sources.get("workingnomads").model_copy(update={"categories": categories})
    ctx = FetchContext(source=source, http=make_http(), budget=RequestBudget(), now=NOW)
    with ctx.http:
        postings = FETCHERS["workingnomads"].fetch(ctx)
    assert {p.raw["category_name"] for p in postings} == kept


def test_findwork_sends_token_and_follows_pages(
    app_config: AppConfig, make_http: Callable[..., HttpClient]
) -> None:
    """Shape checked against the live API on 2026-10-14."""
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
        "source": "Weworkremotely",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers.get("authorization"))
        if "page=2" in str(request.url):
            # Relisted HN comments are skipped: hn_whoishiring has them. Some have no role.
            hn = {**item, "id": 9, "source": "Hn"}
            results = [{**item, "id": 8, "role": "R&amp;D Lead"}, hn, {**hn, "role": None}]
            return httpx.Response(200, json={"next": None, "results": results})
        return httpx.Response(
            200, json={"next": "https://findwork.dev/api/jobs/?page=2", "results": [item]}
        )

    http = make_http({"https://findwork.dev/api/jobs/": handler})
    postings = fetch(app_config, http, "findwork", environ={"FINDWORK_API_KEY": "secret"})
    assert [p.source_job_id for p in postings] == ["7", "8"]
    assert postings[1].title == "R&D Lead"
    assert seen_auth == ["Token secret", "Token secret"]
    assert postings[0].remote_raw == "remote"
    assert postings[0].description_text == "Build things"
