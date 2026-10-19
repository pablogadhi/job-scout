"""Per-ATS knowledge: where a board's token appears in URLs/HTML, and how its jobs map.

API URL templates live in sources.yaml (`ats_platforms`); this module knows the payloads.
Shapes checked against live responses on 2026-10-12 (Greenhouse also 2026-10-09; Workday,
Jobvite and iCIMS 2026-10-15).
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import quote, unquote

import httpx

from job_scout.fetch.base import labelled_salary
from job_scout.fetch.models import Posting
from job_scout.fetch.normalize import Salary, as_float, html_to_text, iso_to_utc
from job_scout.fetch.pages import as_list, job_posting_ld, ld_place, ld_salary

log = logging.getLogger(__name__)

_TOKEN = r"([A-Za-z0-9][A-Za-z0-9_.-]*)"
# Path segments and subdomains that look like a token but aren't one: apply.workable.com/j/<job>,
# and Recruitee's own hosts, which careers pages load scripts from (careers-analytics).
_NOT_TOKENS = {
    *("j", "api", "embed", "v0", "v1", "www", "apply", "jobs", "careers", "static"),
    *("careers-analytics", "app", "cdn", "assets"),
}


@dataclass(frozen=True)
class Board:
    """One company's job board to poll."""

    slug: str  # ats_companies.slug
    company: str
    ats: str
    token: str
    api_url: str


class Requests(Protocol):
    """How a platform reads a board: FetchContext when polling, the Detector when detecting."""

    def get(self, url: str) -> httpx.Response: ...
    def get_json(self, url: str) -> Any: ...
    def post_json(self, url: str, body: Any) -> Any: ...


@dataclass(frozen=True)
class Platform:
    name: str
    token_patterns: tuple[re.Pattern[str], ...]
    jobs: Callable[[Any], list[Any]]
    map_job: Callable[[Any, Board], Posting | None]
    # A board whose API isn't one GET (Workday: POST pages): (requests, api url, first page
    # only) -> the board's jobs.
    list_jobs: Callable[[Requests, str, bool], list[Any]] | None = None
    # Completes a token found in a URL (Workday's API tenant is on the board page).
    resolve: Callable[[Requests, str], str] | None = None
    # One more request per job for what the list lacks (Workday: the description).
    details: Callable[[Requests, Board, Any], Any] | None = None
    probe: bool = True  # a company slug can be tried as a token

    def read(self, requests: Requests, api: str, *, first_page_only: bool = False) -> list[Any]:
        if self.list_jobs:
            return self.list_jobs(requests, api, first_page_only)
        return self.jobs(requests.get_json(api))


def api_url(template: str, token: str) -> str:
    """Fill the template's placeholders with the URL-quoted token: a single placeholder takes
    the whole token, several take its "/"-separated parts in order (Workday: host/tenant/site)."""
    placeholders = re.findall(r"\{\w+\}", template)
    parts = [token] if len(placeholders) == 1 else token.split("/")
    if len(parts) != len(placeholders):
        raise ValueError(f"token {token!r} doesn't fit {template}")
    values = iter(parts)
    return re.sub(r"\{\w+\}", lambda _: quote(next(values), safe=""), template)


def tokens_in(text: str, platform: Platform) -> Iterable[str]:
    for pattern in platform.token_patterns:
        for match in pattern.finditer(text):
            token = "/".join(group.rstrip(".") for group in match.groups() if group)
            if token.lower() not in _NOT_TOKENS:
                yield token


def _tags(*values: Any) -> list[str]:
    """Tags without the missing ones (a job may have no department or team)."""
    return [str(v) for v in values if v]


def _ms(value: Any) -> datetime | None:
    return datetime.fromtimestamp(int(value) / 1000, UTC) if value else None


def _join(parts: Iterable[str | None], sep: str = "; ") -> str | None:
    seen = dict.fromkeys(p.strip() for p in parts if p and p.strip())
    return sep.join(seen) or None


def _with_salary(posting: Posting, salary: Salary | None) -> Posting:
    """Structured salary when the ATS has one; otherwise a labelled line in the text."""
    return posting.with_salary(salary or labelled_salary(posting.description_text))


def _salary(minimum: Any, maximum: Any, currency: Any, period: str | None) -> Salary | None:
    low, high = as_float(minimum), as_float(maximum)
    if not (low or high) or not currency or period is None:
        return None
    return Salary(min=low, max=high, currency=str(currency).upper(), period=period)


# --- Greenhouse --------------------------------------------------------------------------
# GET boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true -> {"jobs": [...]}
# `content` is HTML with its entities escaped (html_to_text does both passes).


def _map_greenhouse(job: dict[str, Any], board: Board) -> Posting:
    content = job.get("content")
    description = html_to_text(html.unescape(content) if content else None)  # always escaped
    offices = [o.get("name") for o in job.get("offices") or []]
    posting = Posting(
        source_job_id=f"greenhouse:{board.token}:{job['id']}",
        canonical_url=job["absolute_url"],
        apply_url=job["absolute_url"],
        company=job.get("company_name") or board.company,
        title=job["title"],
        location_raw=_join([(job.get("location") or {}).get("name"), *offices]),
        description_text=description,
        tags=_tags("ats: greenhouse", *(d.get("name") for d in job.get("departments") or [])),
        posted_at=iso_to_utc(job.get("first_published") or job.get("updated_at")),
        raw={k: v for k, v in job.items() if k != "content"},
    )
    return _with_salary(posting, None)


# --- Lever -------------------------------------------------------------------------------
# GET api.lever.co/v0/postings/{company}?mode=json -> [...]

_LEVER_PERIODS = {
    "per-year-salary": "year",
    "per-month-salary": "month",
    "per-week-salary": "week",
    "per-day-wage": "day",
    "per-hour-wage": "hour",
}


def _map_lever(job: dict[str, Any], board: Board) -> Posting:
    categories = job.get("categories") or {}
    lists = [
        f"{item.get('text', '')}\n{html_to_text(item.get('content')) or ''}"
        for item in job.get("lists") or []
    ]
    description = _join(
        [
            job.get("descriptionPlain") or job.get("descriptionBodyPlain"),
            *lists,
            job.get("additionalPlain"),
        ],
        sep="\n\n",
    )
    salary_range = job.get("salaryRange") or {}
    posting = Posting(
        source_job_id=f"lever:{board.token}:{job['id']}",
        canonical_url=job["hostedUrl"],
        apply_url=job.get("applyUrl"),
        company=board.company,
        title=job["text"],
        location_raw=_join(categories.get("allLocations") or [categories.get("location")]),
        remote_raw=job.get("workplaceType") if job.get("workplaceType") != "unspecified" else None,
        description_text=description,
        tags=_tags(
            "ats: lever",
            categories.get("department"),
            categories.get("team"),
            categories.get("commitment"),
        ),
        posted_at=_ms(job.get("createdAt")),
        raw={k: v for k, v in job.items() if k not in ("description", "descriptionBody")},
    )
    return _with_salary(
        posting,
        _salary(
            salary_range.get("min"),
            salary_range.get("max"),
            salary_range.get("currency"),
            _LEVER_PERIODS.get(salary_range.get("interval", "")),
        ),
    )


# --- Ashby -------------------------------------------------------------------------------
# GET api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true -> {"jobs": [...]}

_ASHBY_PERIODS = {
    "1 YEAR": "year",
    "1 MONTH": "month",
    "1 WEEK": "week",
    "1 DAY": "day",
    "1 HOUR": "hour",
}


def _ashby_salary(job: dict[str, Any]) -> Salary | None:
    for part in (job.get("compensation") or {}).get("summaryComponents") or []:
        if part.get("compensationType") == "Salary":
            return _salary(
                part.get("minValue"),
                part.get("maxValue"),
                part.get("currencyCode"),
                _ASHBY_PERIODS.get(str(part.get("interval", "")).upper()),
            )
    return None


def _map_ashby(job: dict[str, Any], board: Board) -> Posting | None:
    if job.get("isListed") is False:
        return None
    country = ((job.get("address") or {}).get("postalAddress") or {}).get("addressCountry")
    workplace = job.get("workplaceType") or ("Remote" if job.get("isRemote") else None)
    posting = Posting(
        source_job_id=f"ashby:{board.token}:{job['id']}",
        canonical_url=job["jobUrl"],
        apply_url=job.get("applyUrl"),
        company=board.company,
        title=job["title"],
        location_raw=_join(
            [
                job.get("location"),
                *(s.get("location") for s in job.get("secondaryLocations") or []),
                country,
            ]
        ),
        remote_raw=workplace,
        description_text=job.get("descriptionPlain") or html_to_text(job.get("descriptionHtml")),
        tags=_tags("ats: ashby", job.get("department"), job.get("team"), job.get("employmentType")),
        posted_at=iso_to_utc(job.get("publishedAt")),
        raw={k: v for k, v in job.items() if k not in ("descriptionHtml", "descriptionPlain")},
    )
    return _with_salary(posting, _ashby_salary(job))


# --- Workable ----------------------------------------------------------------------------
# GET apply.workable.com/api/v1/widget/accounts/{account}?details=true -> {"jobs": [...]}


def _map_workable(job: dict[str, Any], board: Board) -> Posting:
    locations = [
        ", ".join(p for p in (loc.get("city"), loc.get("region"), loc.get("country")) if p)
        for loc in job.get("locations") or []
    ] or [", ".join(p for p in (job.get("city"), job.get("state"), job.get("country")) if p)]
    published = job.get("published_on") or job.get("created_at")
    posting = Posting(
        source_job_id=f"workable:{board.token}:{job['shortcode']}",
        canonical_url=job.get("url") or job["shortlink"],
        apply_url=job.get("application_url"),
        company=board.company,
        title=job["title"],
        location_raw=_join(locations),
        remote_raw="remote" if job.get("telecommuting") else None,
        description_text=html_to_text(job.get("description")),
        tags=_tags("ats: workable", job.get("department"), job.get("employment_type")),
        posted_at=iso_to_utc(f"{published}T00:00:00+00:00") if published else None,
        raw={k: v for k, v in job.items() if k != "description"},
    )
    return _with_salary(posting, None)


# --- Recruitee ---------------------------------------------------------------------------
# GET {company}.recruitee.com/api/offers/ -> {"offers": [...]}

_RECRUITEE_PERIODS = {
    "year": "year",
    "month": "month",
    "week": "week",
    "day": "day",
    "hour": "hour",
}


def _map_recruitee(job: dict[str, Any], board: Board) -> Posting | None:
    if job.get("status", "published") != "published":
        return None
    workplace = _join(
        [
            "remote" if job.get("remote") else None,
            "hybrid" if job.get("hybrid") else None,
            "on-site" if job.get("on_site") else None,
        ],
        sep=", ",
    )
    salary = job.get("salary") or {}
    published = job.get("published_at") or job.get("created_at")
    posting = Posting(
        source_job_id=f"recruitee:{board.token}:{job['id']}",
        canonical_url=job["careers_url"],
        apply_url=job.get("careers_apply_url"),
        company=job.get("company_name") or board.company,
        title=job["title"],
        location_raw=_join(
            [loc.get("name") for loc in job.get("locations") or []] or [job.get("location")]
        ),
        remote_raw=workplace,
        description_text=_join(
            [html_to_text(job.get("description")), html_to_text(job.get("requirements"))],
            sep="\n\n",
        ),
        tags=_tags("ats: recruitee", job.get("department"), job.get("employment_type_code")),
        posted_at=iso_to_utc(published.replace(" UTC", "+00:00")) if published else None,
        raw={k: v for k, v in job.items() if k not in ("description", "requirements")},
    )
    return _with_salary(
        posting,
        _salary(
            salary.get("min"),
            salary.get("max"),
            salary.get("currency"),
            _RECRUITEE_PERIODS.get(str(salary.get("period") or "")),
        ),
    )


# --- Workday -----------------------------------------------------------------------------
# POST https://{host}/wday/cxs/{tenant}/{site}/jobs with {"appliedFacets": {}, "limit": 20,
# "offset": n, "searchText": ""} -> {"total": N, "jobPostings": [...]}, 20 at most per page;
# only the first page's total is used. List items have title, externalPath, locationsText
# ("10 Locations" when there are several) and a relative postedOn. A job's description:
# GET https://{host}/wday/cxs/{tenant}/{site}{externalPath} -> {"jobPostingInfo": {...}}.
# The API's tenant isn't always the host's (osv-rtr.wd5.myworkdayjobs.com answers as
# osv_rtr), so it is read from the board page's config. No slug probing: a board's host
# (wd1, wd3, wd5 …) and site name can't be guessed.

WORKDAY_PAGE_SIZE = 20
WORKDAY_MAX_PAGES = 50  # 1,000 jobs
_WORKDAY_CONFIG = {key: re.compile(rf'\b{key}:\s*"([^"]+)"') for key in ("tenant", "siteId")}
_WORKDAY_LOCATION_COUNT = re.compile(r"^\d+ Locations?$", re.I)


def _workday_jobs(requests: Requests, api: str, first_page_only: bool) -> list[Any]:
    items: list[Any] = []
    total: int | None = None
    for _ in range(WORKDAY_MAX_PAGES):
        body = {
            "appliedFacets": {},
            "limit": WORKDAY_PAGE_SIZE,
            "offset": len(items),
            "searchText": "",
        }
        payload = requests.post_json(api, body) or {}
        page = payload.get("jobPostings")
        if not isinstance(page, list):
            raise ValueError("expected a list under 'jobPostings'")
        if total is None:
            total = int(payload.get("total") or 0)
        items.extend(page)
        if first_page_only or not page or len(items) >= total:
            break
    return items


def _workday_resolve(requests: Requests, token: str) -> str:
    """host/site or host/recruiting/tenant/site, as found in a URL -> host/tenant/site, from
    the board page. A complete token comes back as it is."""
    parts = token.split("/")
    if len(parts) == 3:
        return token
    page = requests.get(f"https://{token}").text
    tenant, site = (_WORKDAY_CONFIG[key].search(page) for key in ("tenant", "siteId"))
    if not (tenant and site):
        raise ValueError(f"no Workday tenant and site on https://{token}")
    return f"{parts[0]}/{tenant.group(1)}/{site.group(1)}"


def _workday_details(requests: Requests, board: Board, job: dict[str, Any]) -> dict[str, Any]:
    payload = requests.get_json(f"{board.api_url.removesuffix('/jobs')}{job['externalPath']}")
    return {**job, "detail": (payload or {}).get("jobPostingInfo") or {}}


def _workday_page_url(board: Board, path: str) -> str:
    host, tenant, site = board.token.split("/")
    recruiting = f"recruiting/{tenant}/" if host.endswith(".myworkdaysite.com") else ""
    return f"https://{host}/{recruiting}{site}{path}"


def _map_workday(job: dict[str, Any], board: Board) -> Posting:
    """Without details (a job already stored, or a title the filters drop) only the list's
    fields: no description, and no location when the list just counts them."""
    info: dict[str, Any] = job.get("detail") or {}
    path = job["externalPath"]
    url = info.get("externalUrl") or _workday_page_url(board, path)
    listed = job.get("locationsText")
    locations = (
        [info.get("location"), *(info.get("additionalLocations") or [])]
        if info
        else [listed if listed and not _WORKDAY_LOCATION_COUNT.match(listed) else None]
    )
    start = info.get("startDate")
    details = {k: v for k, v in info.items() if k != "jobDescription"}
    posting = Posting(
        source_job_id=f"workday:{board.token}:{path.rsplit('_', 1)[-1]}",
        canonical_url=url,
        apply_url=url,
        company=board.company,
        title=job["title"],
        location_raw=_join(locations),
        remote_raw=info.get("remoteType") or job.get("remoteType"),
        description_text=html_to_text(info.get("jobDescription")),
        tags=_tags("ats: workday", info.get("timeType")),
        posted_at=iso_to_utc(f"{start}T00:00:00+00:00") if start else None,
        raw={**{k: v for k, v in job.items() if k != "detail"}, "detail": details},
    )
    return _with_salary(posting, None)


# --- Job pages with a schema.org JobPosting (Jobvite, iCIMS) ----------------------------
# Neither has a public jobs API: the board lists its jobs (Jobvite's jobs page, iCIMS's
# sitemap), and each job's page carries a JobPosting in JSON-LD (title, description,
# datePosted, jobLocation, employmentType, sometimes baseSalary and jobLocationType). A list
# item: {"id", "title", "url" (the job's page), "page" (what to fetch for its JobPosting)}.


def _page_posting(requests: Requests, board: Board, job: dict[str, Any]) -> dict[str, Any]:
    return {**job, "posting": job_posting_ld(requests.get(job["page"]).text) or {}}


def _map_job_page(job: dict[str, Any], board: Board) -> Posting:
    """Without its page's JobPosting (a job already stored, or a title the filters drop) only
    the list's fields."""
    ld: dict[str, Any] = job.get("posting") or {}
    applicants = [
        f"Remote: {a.get('name')}"
        for a in as_list(ld.get("applicantLocationRequirements"))
        if isinstance(a, dict) and a.get("name")
    ]
    description = ld.get("description")
    posting = Posting(
        source_job_id=f"{board.ats}:{board.token}:{job['id']}",
        canonical_url=job["url"],
        apply_url=job["url"],
        company=board.company,
        title=html.unescape(ld.get("title") or job["title"]),
        location_raw=_join([*(ld_place(p) for p in as_list(ld.get("jobLocation"))), *applicants]),
        remote_raw="remote" if ld.get("jobLocationType") == "TELECOMMUTE" else None,
        description_text=html_to_text(html.unescape(description)) if description else None,
        tags=_tags(f"ats: {board.ats}", *as_list(ld.get("employmentType"))),
        posted_at=iso_to_utc(ld.get("datePosted")),
        raw={
            **{k: v for k, v in job.items() if k != "posting"},
            "posting": {k: v for k, v in ld.items() if k != "description"},
        },
    )
    return _with_salary(posting, ld_salary(ld))


# Jobvite: GET jobs.jobvite.com/{company}/jobs, an HTML page linking every job as
# <a href="/{company}/job/{id}">Title</a>. Only that page is read: a board long enough for
# Jobvite's pagination is logged, since its next pages were never seen to model.
_JOBVITE_LINK = re.compile(r'<a[^>]+href="(/[^/"]+/job/([A-Za-z0-9]+))"[^>]*>\s*([^<]+?)\s*</a>')


def _jobvite_jobs(requests: Requests, api: str, first_page_only: bool) -> list[Any]:
    page = requests.get(api).text
    if "jv-pagination" in page:
        log.warning("%s has more pages; only the first is read", api)
    jobs: dict[str, dict[str, Any]] = {}
    for path, job_id, title in _JOBVITE_LINK.findall(page):
        url = f"https://jobs.jobvite.com{path}"
        jobs.setdefault(
            job_id, {"id": job_id, "title": html.unescape(title), "url": url, "page": url}
        )
    return list(jobs.values())


# iCIMS: GET {portal}.icims.com/sitemap.xml lists every open job as
# https://{portal}.icims.com/jobs/{id}/{slug}/job; the page's JobPosting is in its
# ?in_iframe=1 view (the page itself only frames it). The slug stands in for the title
# until then (filters.yaml matches it case-insensitively).
_ICIMS_JOB = re.compile(r"<loc>\s*(https?://[^<\s]+/jobs/(\d+)/([^/<\s]+)/job)\s*</loc>")


def _icims_jobs(requests: Requests, api: str, first_page_only: bool) -> list[Any]:
    jobs: dict[str, dict[str, Any]] = {}
    for url, job_id, slug in _ICIMS_JOB.findall(requests.get(api).text):
        url = html.unescape(url)
        title = unquote(slug).replace("-", " ")
        jobs.setdefault(
            job_id, {"id": job_id, "title": title, "url": url, "page": f"{url}?in_iframe=1"}
        )
    return list(jobs.values())


def _list(key: str | None) -> Callable[[Any], list[Any]]:
    def jobs(payload: Any) -> list[Any]:
        items = payload if key is None else (payload or {}).get(key)
        if not isinstance(items, list):
            raise ValueError(f"expected a list{f' under {key!r}' if key else ''}")
        return items

    return jobs


def _patterns(*patterns: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p, re.I) for p in patterns)


PLATFORMS: dict[str, Platform] = {
    "greenhouse": Platform(
        "greenhouse",
        _patterns(
            rf"(?:boards|job-boards)\.greenhouse\.io/embed/job_board(?:/js)?\?for={_TOKEN}",
            rf"(?:boards|job-boards)\.greenhouse\.io/(?!embed/){_TOKEN}",
            rf"boards-api\.greenhouse\.io/v1/boards/{_TOKEN}",
        ),
        _list("jobs"),
        _map_greenhouse,
    ),
    "lever": Platform(
        "lever",
        _patterns(rf"jobs\.lever\.co/{_TOKEN}", rf"api\.lever\.co/v0/postings/{_TOKEN}"),
        _list(None),
        _map_lever,
    ),
    "ashby": Platform(
        "ashby",
        _patterns(
            rf"jobs\.ashbyhq\.com/{_TOKEN}", rf"api\.ashbyhq\.com/posting-api/job-board/{_TOKEN}"
        ),
        _list("jobs"),
        _map_ashby,
    ),
    "workable": Platform(
        "workable",
        _patterns(
            rf"apply\.workable\.com/(?:api/v1/widget/accounts/)?{_TOKEN}",
            r"//([A-Za-z0-9-]+)\.workable\.com",
        ),
        _list("jobs"),
        _map_workable,
    ),
    "recruitee": Platform(
        "recruitee",
        _patterns(r"//([A-Za-z0-9-]+)\.recruitee\.com"),
        _list("offers"),
        _map_recruitee,
    ),
    "workday": Platform(
        "workday",
        # host, then the site (optionally after a locale), or recruiting/<tenant>/<site>
        _patterns(
            r"//((?:[a-z0-9-]+\.)?wd\d+\.myworkday(?:jobs|site)\.com)/(?:[a-z]{2}-[a-z]{2}/)?"
            r"(?!wday/)((?:recruiting/[a-z0-9_-]+/)?[a-z0-9_-]+)"
        ),
        _list("jobPostings"),
        _map_workday,
        list_jobs=_workday_jobs,
        resolve=_workday_resolve,
        details=_workday_details,
        probe=False,
    ),
    "jobvite": Platform(
        "jobvite",
        _patterns(r"jobs\.jobvite\.com/(?:careers/)?([A-Za-z0-9_-]+)"),
        _list(None),
        _map_job_page,
        list_jobs=_jobvite_jobs,
        details=_page_posting,
        probe=False,
    ),
    "icims": Platform(
        "icims",
        # careers-<org>.icims.com, not the CDN hosts its pages load from
        _patterns(r"//((?!cdn\d*\.)[A-Za-z0-9-]+)\.icims\.com"),
        _list(None),
        _map_job_page,
        list_jobs=_icims_jobs,
        details=_page_posting,
        probe=False,
    ),
}
