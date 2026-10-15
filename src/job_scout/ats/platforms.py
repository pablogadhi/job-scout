"""Per-ATS knowledge: where a board's token appears in URLs/HTML, and how its jobs map.

API URL templates live in sources.yaml (`ats_platforms`); this module knows the payloads.
Shapes checked against live responses on 2026-10-12 (Greenhouse also 2026-10-09).
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from job_scout.fetch.base import labelled_salary
from job_scout.fetch.models import Posting
from job_scout.fetch.normalize import Salary, as_float, html_to_text, iso_to_utc

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


@dataclass(frozen=True)
class Platform:
    name: str
    token_patterns: tuple[re.Pattern[str], ...]
    jobs: Callable[[Any], list[Any]]
    map_job: Callable[[Any, Board], Posting | None]


def api_url(template: str, token: str) -> str:
    """Fill the template's single {placeholder} with the URL-quoted token."""
    return re.sub(r"\{\w+\}", lambda _: quote(token, safe=""), template, count=1)


def tokens_in(text: str, platform: Platform) -> Iterable[str]:
    for pattern in platform.token_patterns:
        for match in pattern.finditer(text):
            token = match.group(1).rstrip(".")
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
}
