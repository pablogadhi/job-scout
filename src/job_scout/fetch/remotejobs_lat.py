"""RemoteJobs.lat: an HTML board with no feed or API.

A category page lists 50 jobs, newest first, each with its place and whole description.
The job's own page has a schema.org JobPosting with what the list lacks: the salary's
period (the list shows "$2500 - $3000", mostly per month) and the posting date. It's read
once per new job whose title passes filters.yaml. Its applicant countries aren't used: a
job listed as "Remote" or "Remote Latin America" gets the same six (Argentina, Mexico,
Colombia, Chile, Peru, Brazil), and a real list is on the card too. Shapes taken from the
live site on 2026-10-19; synthetic copies live in tests/fixtures/http/remotejobs_lat/.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser, LexborNode

from job_scout.fetch.base import FetchContext, collect, with_details
from job_scout.fetch.models import Posting
from job_scout.fetch.normalize import html_to_text, iso_to_utc
from job_scout.fetch.pages import job_posting_ld, ld_salary

MAX_PAGES = 5  # per category, when sources.yaml sets no max_pages
_JOB_ID = re.compile(r"^heading-job-(\d+)$")
_AGO = re.compile(r"(\d+)\s+(minute|hour|day|week|month|year)s?\s+ago", re.I)
_UNIT = {"minute": 1 / 1440, "hour": 1 / 24, "day": 1, "week": 7, "month": 30, "year": 365}


def _text(node: LexborNode | None) -> str | None:
    return (node.text(strip=True) or None) if node else None


def _card(article: LexborNode, base: str) -> dict[str, Any]:
    """One listed job as a JSON-safe dict (the stored payload)."""
    match = _JOB_ID.match(article.id or "")
    link = article.css_first("a.rj-job-title")
    if not match or link is None:
        raise ValueError("a job card without an id or a title link")
    meta = [_text(s) for s in article.css(".rj-job-meta span")]
    body = article.css_first(".rj-prose")
    return {
        "id": match.group(1),
        "url": urljoin(base, link.attributes.get("href") or ""),
        "title": _text(link),
        "company": _text(article.css_first(".rj-job-meta strong")),
        # The place has a map-pin icon; the other span is the job type.
        "location": next(
            (_text(s) for s in article.css(".rj-job-meta span") if s.css_first("i.bi-geo-alt")),
            None,
        ),
        "meta": [m for m in meta if m],
        "salary": _text(article.css_first(".rj-pill-salary")),
        "ago": _text(article.css_first(".rj-job-side .rj-muted")),
        "category": article.attributes.get("data-category"),
        "tags": [t for p in article.css(".rj-job-tags .rj-pill") if (t := _text(p))],
        "description_html": body.html if body else None,
    }


def _posted(ago: str | None, now: datetime) -> datetime | None:
    """'10 days ago' -> about when; the job's page has the exact date."""
    match = _AGO.search(ago or "")
    if not match:
        return None
    return now - timedelta(days=int(match.group(1)) * _UNIT[match.group(2).lower()])


def map_card(card: dict[str, Any], now: datetime) -> Posting:
    """The listed job; without its page there is no salary (the list gives no period)."""
    location = card.get("location")
    return Posting(
        source_job_id=card["id"],
        canonical_url=card["url"],
        title=card["title"],
        company=card.get("company"),
        location_raw=location,
        # Every job on the board is remote; "Remote" is also the place most cards show.
        remote_raw="remote",
        description_text=html_to_text(card.get("description_html")),
        tags=[*card.get("tags", []), *(m for m in card.get("meta", []) if m != location)],
        posted_at=_posted(card.get("ago"), now),
        raw=card,
    )


def _job_page(ctx: FetchContext, posting: Posting) -> Posting:
    """The page's JobPosting: the salary with its period, and the posting date. The list's
    description stays (the JobPosting's has lost its line breaks)."""
    ld = job_posting_ld(ctx.get(posting.canonical_url).text) or {}
    update: dict[str, Any] = {
        "raw": {**posting.raw, "page": {k: v for k, v in ld.items() if k != "description"}},
        "posted_at": iso_to_utc(ld.get("datePosted")) or posting.posted_at,
    }
    return posting.model_copy(update=update).with_salary(ld_salary(ld))


@dataclass
class RemoteJobsLatFetcher:
    def fetch(self, ctx: FetchContext) -> list[Posting]:
        postings: dict[str, Posting] = {}  # a job listed in two categories is read once
        for url in ctx.source.endpoints:
            for page in range(1, (ctx.source.max_pages or MAX_PAGES) + 1):
                tree = LexborHTMLParser(ctx.get(url, params={"page": page}).text)
                articles = tree.css("article.rj-job")
                found = list(
                    collect(ctx, articles, lambda a, url=url: map_card(_card(a, url), ctx.now))
                )
                for posting in found:
                    postings.setdefault(posting.source_job_id, posting)
                # Newest first: a page with no job we haven't stored ends this category.
                fresh = any(p.source_job_id not in ctx.known_ids for p in found)
                if not fresh or tree.css_first("a.next_page") is None:
                    break
        return with_details(ctx, list(postings.values()), _job_page)
