"""Per-source mappers for the Tier 1 API and RSS sources, and the fetcher registry (HTML
scrapers have a module each).

Each mapper turns one payload item into a Posting. Shapes were taken from live payloads
on 2026-10-12; recorded samples live in tests/fixtures/http/<source_id>/.
"""

from __future__ import annotations

import html
import math
import re
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

from selectolax.lexbor import LexborHTMLParser

from job_scout.ats.fetcher import AtsFetcher
from job_scout.config.secrets import secret
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
    as_float,
    fix_mojibake,
    html_to_text,
    iso_to_utc,
    parse_salary,
)
from job_scout.fetch.pages import job_posting_ld, ld_locations
from job_scout.fetch.remotejobs_lat import RemoteJobsLatFetcher

# --- 4dayweek (JSON, paginated) ----------------------------------------------------------

FOURDAYWEEK_MAX_PAGES = 30
# Minimum `posted_after` window (days). Wider than the gap between runs, so a job that shows
# up on the board with a posting date a few days old isn't missed; the overlap is "seen".
FOURDAYWEEK_MIN_WINDOW_DAYS = 7


def _fourdayweek_pages(ctx: FetchContext, url: str) -> Iterator[Any]:
    params: dict[str, Any] = {}
    if ctx.last_success_at is not None:
        # Incremental after the first run: postings from the last N days ("posted within
        # the last N days" in their OpenAPI spec), never less than the minimum window.
        days = math.ceil((ctx.now - ctx.last_success_at).total_seconds() / 86400) + 1
        params["posted_after"] = min(max(days, FOURDAYWEEK_MIN_WINDOW_DAYS), 365)
    for page in range(1, FOURDAYWEEK_MAX_PAGES + 1):
        payload = ctx.get_json(url, params={**params, "page": page})
        yield payload
        if not payload.get("has_more"):
            return
    raise FetchError(f"more than {FOURDAYWEEK_MAX_PAGES} pages; raise FOURDAYWEEK_MAX_PAGES")


def _fourdayweek_location(item: dict[str, Any]) -> str | None:
    """A remote location with a country is where applicants must live: the job's page shows
    it as "Must be based in: USA". A remote one with only a continent stays a region."""
    parts, based_in = [], []
    for loc in item.get("locations") or []:
        if loc.get("work_arrangement") == "remote" and loc.get("country"):
            based_in.append(loc["country"])
            continue
        place = ", ".join(p for p in (loc.get("city"), loc.get("state"), loc.get("country")) if p)
        place = place or loc.get("continent") or "unspecified"
        parts.append(f"{place} ({loc.get('work_arrangement', '?')})")
    if based_in:
        parts.insert(0, f"remote, must be based in {' or '.join(dict.fromkeys(based_in))}")
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
    text: dict[str, Any] = {
        k: fix_mojibake(v) if isinstance(v, str) else v for k, v in item.items()
    }
    salary = None
    if text.get("salary_max") or text.get("salary_min"):
        salary = Salary(
            min=as_float(text.get("salary_min")) or None,
            max=as_float(text.get("salary_max")) or as_float(text.get("salary_min")),
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


# --- Working Nomads (JSON, all jobs in one array) -------------------------------------------


def _workingnomads_keep(ctx: FetchContext, item: dict[str, Any]) -> bool:
    wanted = ctx.source.categories
    return not wanted or item.get("category_name") in wanted


def _map_workingnomads(item: dict[str, Any]) -> Posting | None:
    match = re.search(r"/job/go/(\d+)", item["url"])
    description = html_to_text(item.get("description"))
    return Posting(
        source_job_id=match.group(1) if match else item["url"],
        canonical_url=item["url"],
        title=item["title"],
        company=item.get("company_name"),
        location_raw=item.get("location"),
        description_text=description,
        tags=str(item.get("tags") or "").split(","),
        posted_at=iso_to_utc(item.get("pub_date")),
        raw=item,
    ).with_salary(labelled_salary(description))


# --- Findwork (JSON, API key, paginated) -------------------------------------------------

FINDWORK_MAX_PAGES = 5  # when sources.yaml sets no max_pages


def _findwork_pages(ctx: FetchContext, url: str) -> Iterator[Any]:
    key = secret(ctx.environ, ctx.source.auth_env or "")
    if key is None:
        raise FetchError(f"{ctx.source.auth_env} is not set")
    headers = {"Authorization": f"Token {key.get_secret_value()}"}
    next_url: str | None = url
    for _ in range(ctx.source.max_pages or FINDWORK_MAX_PAGES):
        if not next_url:
            return
        payload = ctx.get_json(next_url, headers=headers)
        yield payload
        next_url = payload.get("next")


def _map_findwork(item: dict[str, Any]) -> Posting | None:
    # Findwork relists HN "Who is hiring" comments, which hn_whoishiring fetches directly.
    # Its copies have a shorter title (often none), so they'd never merge with ours and the
    # same job would be scored twice.
    if item.get("source") == "Hn":
        return None
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


def _from_job_page(posting: Posting, page: str, body: str | None = None) -> Posting:
    """The feed's posting completed from its job page: the full description (`body`, the
    page's posting HTML, or else its JobPosting's), the JobPosting's places and the countries
    applicants must be in. `raw["page"]` marks a job whose page was read."""
    ld = job_posting_ld(page) or {}
    description = html_to_text(body) or (
        html_to_text(html.unescape(ld["description"])) if ld.get("description") else None
    )
    update: dict[str, Any] = {
        "raw": {**posting.raw, "page": {k: v for k, v in ld.items() if k != "description"}}
    }
    if description:
        update["description_text"] = description
    if locations := ld_locations(ld):
        update["location_raw"] = "; ".join(locations)
    if ld.get("jobLocationType") == "TELECOMMUTE" and not posting.remote_raw:
        update["remote_raw"] = "remote"
    return posting.model_copy(update=update)


def _pyjobs_page(ctx: FetchContext, posting: Posting) -> Posting:
    """The feed has one line per job; the page's JobPosting has the description and, for
    "Geo remote" jobs, the countries applicants must be in."""
    return _from_job_page(posting, ctx.get(posting.canonical_url).text)


def _typescriptjobs_page(ctx: FetchContext, posting: Posting) -> Posting:
    """The feed has a two-sentence summary, and so does the page's JobPosting: the posting
    itself is the page's `.prose` block."""
    page = ctx.get(posting.canonical_url).text
    body = LexborHTMLParser(page).css_first("div.prose")
    return _from_job_page(posting, page, body.html if body else None)


def _nodesk_page(ctx: FetchContext, posting: Posting) -> Posting:
    """The page's JobPosting has the description, but its applicant location is "Anywhere"
    even for a job in one country: the place is the page's location line ("Remote" over
    "India", "Worldwide" or nothing). The page's tags name the stack."""
    page = ctx.get(posting.canonical_url).text
    posting = _from_job_page(posting, page)
    tree = LexborHTMLParser(page)
    line = tree.css_first("div.dtc.grey-700")
    place = [t for p in line.css("p") if (t := p.text(strip=True))] if line else []
    main = tree.css_first("main[data-job-tags]")
    tags = str(main.attributes.get("data-job-tags") or "").split(", ") if main else []
    return posting.model_copy(
        update={
            "location_raw": ", ".join(place) or None,
            "tags": [*posting.tags, *(t for t in tags if t)],
            "raw": {**posting.raw, "page": {**posting.raw["page"], "location_line": place}},
        }
    )


def _map_title_at_company(entry: FeedEntry) -> Posting:
    """RWFA, Remote First Jobs, TypeScriptJobs, NoDesk: 'Title at Company', author =
    company if set."""
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
    r"^(full[- ]time|part[- ]time|internship|contract(or)?|freelance|temporary)$", re.I
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


def _map_remotive(entry: FeedEntry) -> Posting:
    # jobId is the id the JSON API used, so jobs stored from the API still match.
    return rss_posting(
        entry,
        title=entry["title"],
        company=entry.get("company"),
        source_job_id=entry.get("jobid") or entry["link"],
        location_raw=entry.get("location"),
        extra_tags=[entry.get("type")],
    )


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


# --- ReliefWeb (RSS) -----------------------------------------------------------------------

# The description opens with the duty station ("Country: Kenya") and the organization. A job
# with no country is in the site's "Remote / Roster / Roving" list.
_RELIEFWEB_COUNTRY = re.compile(r'<div class="tag country">Country:\s*(?P<country>[^<]+)</div>')
_RELIEFWEB_JOB_ID = re.compile(r"/job/(?P<id>\d+)")


def _map_reliefweb(entry: FeedEntry) -> Posting:
    country = _RELIEFWEB_COUNTRY.search(entry.get("summary") or "")
    # The numeric id is the one the ReliefWeb API uses.
    job_id = _RELIEFWEB_JOB_ID.search(entry["link"])
    return rss_posting(
        entry,
        title=entry["title"],
        company=entry.get("author"),
        source_job_id=job_id["id"] if job_id else entry["link"],
        location_raw=html.unescape(country["country"]) if country else None,
    )


# --- unvacancies.org (RSS) ---------------------------------------------------------------

# Feed filters as the site's partner kit builds them: remote, function, contract_type, grade,
# location, organization. Items carry only a title, the agency and a one-line summary.


def _map_unvacancies(entry: FeedEntry) -> Posting:
    agency = next((t["term"] for t in entry.get("tags", []) if t.get("term")), None)
    # The guid is the link without its tracking parameters.
    url = entry.get("id") or entry["link"]
    return rss_posting(entry, title=entry["title"], company=agency, canonical_url=url)


def _unvacancies_shared(url: str) -> dict[str, Any]:
    if parse_qs(urlsplit(url).query).get("remote") == ["true"]:
        return {"remote_raw": "Remote and home-based"}  # the site's label for this filter
    return {}


def _unvacancies_page(ctx: FetchContext, posting: Posting) -> Posting:
    """The page has what the feed's one line lacks: the place ("Remote (linked to Bonn,
    Germany)"), who can apply ("Nationals of Germany only"), the contract, experience and
    languages, the work mode and the official notice. The closing date is left out: the
    page words it relative to today ("tomorrow")."""
    tree = LexborHTMLParser(ctx.get(posting.canonical_url).text)
    header = [
        text
        for item in tree.css("[data-job-header-meta-item]")
        if (text := item.text(separator=" ", strip=True)) and not text.startswith("Posted")
    ]
    facts: dict[str, str] = {}
    eligibility = None
    for cell in tree.css("[data-decision-strip] [data-cell]"):
        label = cell.css_first("dt")
        values = [t for dd in cell.css("dd") if (t := dd.text(separator=" ", strip=True))]
        kind = cell.attributes.get("data-cell")
        if label and values and kind != "closes":
            facts[label.text(strip=True)] = "; ".join(values)
            if kind == "eligibility":
                eligibility = f"who can apply: {'; '.join(values)}"
    details = {
        dt.text(strip=True): dd.text(separator=", ", strip=True)
        for dt in tree.css("[data-job-more-details] dt")
        if dt.parent and (dd := dt.parent.css_first("dd"))
    }
    notice = tree.css_first("div.detail-prose-wrap > .prose-v2")
    lines = [f"{label}: {value}" for label, value in facts.items()]
    body = html_to_text(notice.html) if notice else None
    update: dict[str, Any] = {
        "description_text": "\n\n".join(
            p for p in ("\n".join(lines), body or posting.description_text) if p
        ),
        "raw": {**posting.raw, "page": {"header": header, "facts": facts, "details": details}},
    }
    if place := "; ".join(p for p in (*header, eligibility) if p):
        update["location_raw"] = place
    if mode := details.get("Work mode"):
        update["remote_raw"] = mode
    return posting.model_copy(update=update)


# --- Registry ------------------------------------------------------------------------------
# remoteintech (with its seed) or company_boards (without) polls the company boards found by
# `ats detect` (job_scout.ats); sources.yaml enables at most one of them.

FETCHERS: dict[str, Fetcher] = {
    "4dayweek": JsonFetcher(
        items=lambda p: p["data"], map_item=_map_fourdayweek, pages=_fourdayweek_pages
    ),
    "remoteok": JsonFetcher(items=lambda p: p, map_item=_map_remoteok),
    "workingnomads": JsonFetcher(
        items=lambda p: p, map_item=_map_workingnomads, keep=_workingnomads_keep
    ),
    "findwork": JsonFetcher(
        items=lambda p: p["results"], map_item=_map_findwork, pages=_findwork_pages
    ),
    "hn_whoishiring": HackerNewsFetcher(),
    "weworkremotely": RssFetcher(_map_weworkremotely),
    "realworkfromanywhere": RssFetcher(_map_title_at_company),
    "remotefirstjobs": RssFetcher(_map_title_at_company),
    "typescriptjobs": RssFetcher(_map_title_at_company, details=_typescriptjobs_page),
    "pyjobs": RssFetcher(_map_pyjobs, details=_pyjobs_page),
    "nodesk": RssFetcher(_map_title_at_company, details=_nodesk_page),
    "remotegamejobs": RssFetcher(_map_remotegamejobs),
    "findjobit": RssFetcher(_map_findjobit),
    "remotive": RssFetcher(_map_remotive),
    "reliefweb": RssFetcher(_map_reliefweb),
    "unvacancies": RssFetcher(
        _map_unvacancies, shared=_unvacancies_shared, details=_unvacancies_page
    ),
    "remotejobs_lat": RemoteJobsLatFetcher(),
    "remoteintech": AtsFetcher(),
    "company_boards": AtsFetcher(),
}


def has_details(source_id: str) -> bool:
    """The source's list lacks what a job's page has, so each new job's page is read too."""
    fetcher = FETCHERS.get(source_id)
    if isinstance(fetcher, RemoteJobsLatFetcher):
        return True
    return isinstance(fetcher, RssFetcher) and fetcher.details is not None
