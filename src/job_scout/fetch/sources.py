"""Per-source mappers for the Tier 1 API and RSS sources, and the fetcher registry.

Each mapper turns one payload item into a Posting. Shapes were taken from live payloads
on 2026-10-12; recorded samples live in tests/fixtures/http/<source_id>/.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

from job_scout.fetch.base import (
    FeedEntry,
    FetchContext,
    Fetcher,
    JsonFetcher,
    RssFetcher,
    labelled_salary,
    rss_posting,
)
from job_scout.fetch.hn import HackerNewsFetcher
from job_scout.fetch.http import FetchError
from job_scout.fetch.models import Posting
from job_scout.fetch.normalize import (
    Salary,
    fix_mojibake,
    html_to_text,
    iso_to_utc,
    parse_salary,
)

# --- 4dayweek (JSON, paginated) ----------------------------------------------------------

FOURDAYWEEK_MAX_PAGES = 30


def _fourdayweek_pages(ctx: FetchContext, url: str) -> Iterator[Any]:
    params: dict[str, Any] = {}
    if ctx.last_success_at is not None:
        # Incremental after the first run: postings from the last N days, at least 2 so
        # consecutive windows overlap (the overlap comes back as "seen").
        days = math.ceil((ctx.now - ctx.last_success_at).total_seconds() / 86400) + 1
        params["posted_after"] = min(max(days, 2), 365)
    for page in range(1, FOURDAYWEEK_MAX_PAGES + 1):
        payload = ctx.get_json(url, params={**params, "page": page})
        yield payload
        if not payload.get("has_more"):
            return
    raise FetchError(f"more than {FOURDAYWEEK_MAX_PAGES} pages; raise FOURDAYWEEK_MAX_PAGES")


def _fourdayweek_location(item: dict[str, Any]) -> str | None:
    parts = []
    for loc in item.get("locations") or []:
        place = ", ".join(p for p in (loc.get("city"), loc.get("state"), loc.get("country")) if p)
        place = place or loc.get("continent") or "unspecified"
        parts.append(f"{place} ({loc.get('work_arrangement', '?')})")
    if timezones := item.get("timezones"):
        parts.append(f"timezones: {', '.join(timezones)}")
    if (item.get("company") or {}).get("hires_worldwide"):
        parts.append("company hires worldwide")
    return "; ".join(parts) or None


def _map_fourdayweek(item: dict[str, Any]) -> Posting:
    company = item.get("company") or {}
    tags = [s["name"] for key in ("stack", "tools", "skills") for s in item.get(key) or []]
    if item.get("schedule_type"):
        tags.append(f"schedule: {item['schedule_type']}")
    hours = (item.get("hours_per_week_min"), item.get("hours_per_week_max"))
    if any(hours):
        tags.append(f"hours/week: {hours[0]}-{hours[1]}")
    if item.get("level"):
        tags.append(f"level: {item['level']}")
    if company.get("remote_level"):
        # The company's policy, not this job's: kept out of remote_raw so a remote job at a
        # hybrid company isn't filtered as hybrid.
        tags.append(f"company remote level: {company['remote_level']}")
    salary = None
    if item.get("salary_min") or item.get("salary_max"):
        cents = [v / 100 if v else None for v in (item.get("salary_min"), item.get("salary_max"))]
        salary = Salary(
            min=cents[0],
            max=cents[1] or cents[0],
            currency=item.get("salary_currency"),
            period=item.get("salary_period"),
        )
    return Posting(
        source_job_id=item["id"],
        canonical_url=item["url"],
        title=item["title"],
        company=company.get("name"),
        location_raw=_fourdayweek_location(item),
        remote_raw=item.get("work_arrangement"),
        description_text=item.get("description"),  # plain text per the API spec
        tags=tags,
        posted_at=iso_to_utc(item.get("posted_at")),
        raw=item,
    ).with_salary(salary)


# --- Remote OK (JSON) ----------------------------------------------------------------------


def _map_remoteok(item: dict[str, Any]) -> Posting | None:
    if "legal" in item or "id" not in item:
        return None  # first element is API metadata
    text = {k: fix_mojibake(v) if isinstance(v, str) else v for k, v in item.items()}
    salary = None
    if text.get("salary_max") or text.get("salary_min"):
        salary = Salary(
            min=text.get("salary_min") or None,
            max=text.get("salary_max") or text.get("salary_min"),
            currency="USD",
            period="year",
        )
    return Posting(
        source_job_id=str(text["id"]),
        canonical_url=text["url"],
        apply_url=text.get("apply_url"),
        title=text["position"],
        company=text.get("company"),
        location_raw=text.get("location"),
        description_text=html_to_text(text.get("description")),
        tags=text.get("tags") or [],
        posted_at=iso_to_utc(text.get("date")),
        raw=item,
    ).with_salary(salary)


# --- Remotive (JSON) -----------------------------------------------------------------------


def _slugs(node: Any) -> set[str]:
    if isinstance(node, dict):
        found = {node["slug"]} if isinstance(node.get("slug"), str) else set()
        return found.union(*(_slugs(v) for v in node.values()))
    if isinstance(node, list):
        return set().union(*(_slugs(v) for v in node))
    return set()


def _remotive_check_categories(ctx: FetchContext) -> None:
    """On the first successful run, confirm the configured category slugs exist."""
    if ctx.last_success_at is not None:
        return
    valid = _slugs(ctx.get_json("https://remotive.com/api/remote-jobs/categories"))
    wanted = {
        value
        for url in ctx.source.endpoints
        for value in parse_qs(urlsplit(url).query).get("category", [])
    }
    if wanted - valid:
        raise FetchError(
            f"unknown Remotive categories {sorted(wanted - valid)}; valid: {sorted(valid)}"
        )


def _map_remotive(item: dict[str, Any]) -> Posting:
    return Posting(
        source_job_id=str(item["id"]),
        canonical_url=item["url"],
        title=item["title"],
        company=item.get("company_name"),
        location_raw=item.get("candidate_required_location"),
        description_text=html_to_text(item.get("description")),
        tags=[
            *(item.get("tags") or []),
            *filter(None, [item.get("category"), item.get("job_type")]),
        ],
        posted_at=iso_to_utc(item.get("publication_date")),
        raw=item,
    ).with_salary(parse_salary(item.get("salary")))


# --- Working Nomads (JSON, all jobs in one array) -------------------------------------------

WORKINGNOMADS_CATEGORIES = {"Development"}


def _map_workingnomads(item: dict[str, Any]) -> Posting | None:
    if item.get("category_name") not in WORKINGNOMADS_CATEGORIES:
        return None
    match = re.search(r"/job/go/(\d+)", item["url"])
    description = html_to_text(item.get("description"))
    return Posting(
        source_job_id=match.group(1) if match else item["url"],
        canonical_url=item["url"],
        title=item["title"],
        company=item.get("company_name"),
        location_raw=item.get("location"),
        description_text=description,
        tags=(item.get("tags") or "").split(","),
        posted_at=iso_to_utc(item.get("pub_date")),
        raw=item,
    ).with_salary(labelled_salary(description))


# --- Findwork (JSON, API key, paginated) -------------------------------------------------

FINDWORK_MAX_PAGES = 5


def _findwork_pages(ctx: FetchContext, url: str) -> Iterator[Any]:
    key = ctx.env.get(ctx.source.auth_env or "")
    if not key:
        raise FetchError(f"{ctx.source.auth_env} is not set")
    headers = {"Authorization": f"Token {key}"}
    next_url: str | None = url
    for _ in range(FINDWORK_MAX_PAGES):
        if not next_url:
            return
        payload = ctx.get_json(next_url, headers=headers)
        yield payload
        next_url = payload.get("next")


def _map_findwork(item: dict[str, Any]) -> Posting:
    remote = item.get("remote")
    return Posting(
        source_job_id=str(item["id"]),
        canonical_url=item["url"],
        title=item["role"],
        company=item.get("company_name"),
        location_raw=item.get("location"),
        remote_raw=None if remote is None else ("remote" if remote else "not remote"),
        description_text=html_to_text(item.get("text")),
        tags=[*(item.get("keywords") or []), *filter(None, [item.get("employment_type")])],
        posted_at=iso_to_utc(item.get("date_posted")),
        raw=item,
    )


# --- RSS mappers ---------------------------------------------------------------------------


def _split_at_company(title: str, company: str | None = None) -> tuple[str, str | None]:
    """'Senior Engineer at Acme' -> ('Senior Engineer', 'Acme')."""
    if company and title.endswith(f" at {company}"):
        return title[: -len(f" at {company}")], company
    head, sep, tail = title.rpartition(" at ")
    return (head, tail) if sep and head else (title, company)


def _map_weworkremotely(entry: FeedEntry) -> Posting:
    company, sep, title = entry["title"].partition(": ")
    if not sep:
        company, title = None, entry["title"]
    location = "; ".join(
        f"{label}: {entry[key]}"
        for key, label in (("region", "region"), ("country", "countries"))
        if entry.get(key)
    )
    skills = re.split(r",\s*(?:and\s+)?|\s+and\s+", entry.get("skills") or "")  # "A, B, and C"
    return rss_posting(
        entry,
        title=title,
        company=company,
        location_raw=location or None,
        extra_tags=[*skills, entry.get("type")],
    )


def _map_title_at_company(entry: FeedEntry) -> Posting:
    """RWFA, Remote First Jobs, TypeScriptJobs: 'Title at Company', author = company if set."""
    title, company = _split_at_company(entry["title"], entry.get("author"))
    return rss_posting(entry, title=title, company=company)


def _pyjobs_details(summary: str, title: str) -> tuple[str | None, list[str]]:
    """'<title> at Acme GmbH. Geo remote · Berlin · Full-time · EUR 2,000-3,000 per month.'"""
    rest = summary.removeprefix(f"{title} at ").rstrip(".")
    if " · " not in rest:
        return rest or None, []
    split = rest.rfind(". ", 0, rest.index(" · "))
    if split == -1:
        return None, rest.split(" · ")
    return rest[:split], rest[split + 2 :].split(" · ")


_JOB_TYPES = re.compile(
    r"^(full[- ]time|part[- ]time|internship|contract|freelance|temporary)$", re.I
)


def _map_pyjobs(entry: FeedEntry) -> Posting:
    title = html_to_text(entry["title"]) or entry["title"]  # titles carry &amp;
    summary = html_to_text(entry.get("summary")) or ""
    company, parts = _pyjobs_details(summary, title)
    workplace = parts[0] if parts else None
    if workplace == "Office":
        workplace = "on-site (Office)"  # say it in words the remote pre-filter matches
    salary = next((s for p in parts if (s := parse_salary(p))), None)
    job_types = [p for p in parts[1:] if _JOB_TYPES.match(p)]
    location = [p for p in parts[1:] if not _JOB_TYPES.match(p) and not parse_salary(p)]
    return rss_posting(
        entry,
        title=title,
        company=company,
        description_text=summary,
        location_raw=", ".join(location) or None,
        remote_raw=workplace,
        source_job_id=entry["link"],
        tags=job_types,
    ).with_salary(salary)


_GAMEJOBS_TITLE = re.compile(r"^(?P<company>.+?) is hiring (?P<title>.+?)(?:\s*\(Remote Job\))?$")


def _map_remotegamejobs(entry: FeedEntry) -> Posting:
    match = _GAMEJOBS_TITLE.match(entry["title"])
    company, title = (match["company"], match["title"]) if match else (None, entry["title"])
    return rss_posting(entry, title=title, company=company)


_FINDJOBIT_TITLE = re.compile(r"^(?P<company>.+?)\s+busca (?:un|una|a)\s+(?P<title>.+)$")


def _map_findjobit(entry: FeedEntry) -> Posting:
    match = _FINDJOBIT_TITLE.match(entry["title"])
    company, title = (match["company"].strip(), match["title"]) if match else (None, entry["title"])
    return rss_posting(
        entry,
        title=title,
        company=company,
        location_raw=entry.get("region"),
        extra_tags=[entry.get("type")],
    )


# --- Registry ------------------------------------------------------------------------------

FETCHERS: dict[str, Fetcher] = {
    "4dayweek": JsonFetcher(
        items=lambda p: p["data"], map_item=_map_fourdayweek, pages=_fourdayweek_pages
    ),
    "remoteok": JsonFetcher(items=lambda p: p, map_item=_map_remoteok),
    "remotive": JsonFetcher(
        items=lambda p: p["jobs"], map_item=_map_remotive, before=_remotive_check_categories
    ),
    "workingnomads": JsonFetcher(items=lambda p: p, map_item=_map_workingnomads),
    "findwork": JsonFetcher(
        items=lambda p: p["results"], map_item=_map_findwork, pages=_findwork_pages
    ),
    "hn_whoishiring": HackerNewsFetcher(),
    "weworkremotely": RssFetcher(_map_weworkremotely),
    "realworkfromanywhere": RssFetcher(_map_title_at_company),
    "remotefirstjobs": RssFetcher(_map_title_at_company),
    "typescriptjobs": RssFetcher(_map_title_at_company),
    "pyjobs": RssFetcher(_map_pyjobs),
    "remotegamejobs": RssFetcher(_map_remotegamejobs),
    "findjobit": RssFetcher(_map_findjobit),
}
