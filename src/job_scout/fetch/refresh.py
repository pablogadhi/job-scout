"""`job-scout refresh`: apply a mapper fix to a source's stored jobs. Each job is mapped
again from its saved payload (no requests), and a source with a page step (`has_details`)
reads the page of every stored job that hasn't had it. Identity fields never change, so a
job keeps its group; the changed ones are returned for the pre-filter and scoring to redo."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from job_scout.config import AppConfig
from job_scout.db.models import Job
from job_scout.fetch.base import FetchContext, JsonFetcher, RssFetcher
from job_scout.fetch.http import FetchError, HttpClient
from job_scout.fetch.models import Posting
from job_scout.fetch.runner import _budget
from job_scout.fetch.sources import FETCHERS
from job_scout.prefilter.runner import build_prefilter

log = logging.getLogger(__name__)

# What a refresh may change: the text the pre-filter and Claude read, and the payload.
REFRESHED = ("description_text", "location_raw", "remote_raw", "tags", "raw")
# The ones that change a judgment: a job whose any of these changed is judged again.
JUDGED = ("description_text", "location_raw", "remote_raw")


@dataclass
class RefreshOutcome:
    jobs: int = 0  # the source's stored jobs
    remapped: int = 0  # changed by mapping their saved payload again
    pages: int = 0  # completed from their job page
    failed_pages: int = 0
    requests: int = 0
    changed: list[int] = field(default_factory=list)  # canonical ids to judge again


def can_refresh(source_id: str) -> bool:
    return isinstance(FETCHERS.get(source_id), (JsonFetcher, RssFetcher))


def _mapper(source_id: str) -> Callable[[Any], Posting | None]:
    fetcher = FETCHERS[source_id]
    if isinstance(fetcher, JsonFetcher):
        return fetcher.map_item
    assert isinstance(fetcher, RssFetcher)
    return fetcher.map_entry


def _apply(job: Job, posting: Posting) -> bool:
    """Copy the refreshed fields onto the stored job; True when a judged one changed. A
    field the posting lacks is kept: a payload mapped again lacks the fields its feed's URL
    gave (`RssFetcher.shared`)."""
    values = posting.model_dump()
    judged = any(values[name] is not None and getattr(job, name) != values[name] for name in JUDGED)
    for name in REFRESHED:
        if values[name] is not None:
            setattr(job, name, values[name])
    return judged


def refresh_source(
    config: AppConfig,
    session: Session,
    http: HttpClient,
    source_id: str,
    *,
    now: datetime,
    read_pages: bool = True,
) -> RefreshOutcome:
    """Refresh every stored job of `source_id` in `session` (the caller commits). Without
    `read_pages`, only the saved payloads are mapped again; no request is made."""
    source = config.sources.get(source_id)
    fetcher = FETCHERS[source_id]
    map_item = _mapper(source_id)
    details = fetcher.details if isinstance(fetcher, RssFetcher) else None
    prefilter = build_prefilter(config)
    budget = _budget(session, source, now)
    ctx = FetchContext(source=source, http=http, budget=budget, now=now)
    outcome = RefreshOutcome()
    changed: set[int] = set()
    jobs = session.scalars(select(Job).where(Job.source_id == source_id).order_by(Job.id))
    for job in jobs:
        outcome.jobs += 1
        read_page = details is not None and "page" not in (job.raw or {})
        try:
            posting = map_item(job.raw) if job.raw else None
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            log.warning("%s: job %d didn't map again: %s", source_id, job.id, exc)
            posting = None
        # A job whose page was read keeps what the page gave.
        if posting is not None and (details is None or read_page) and _apply(job, posting):
            outcome.remapped += 1
            changed.add(job.duplicate_of or job.id)
        title_ok = prefilter.title_passes(source_id, job.title)
        if read_pages and read_page and details is not None and title_ok:
            stored = Posting(
                source_job_id=job.source_job_id,
                canonical_url=job.canonical_url,
                title=job.title,
                location_raw=job.location_raw,
                remote_raw=job.remote_raw,
                description_text=job.description_text,
                tags=job.tags or [],
                raw=job.raw or {},
            )
            try:
                page = details(ctx, stored)
            except FetchError as exc:
                log.warning("%s: no job page for %s: %s", source_id, job.canonical_url, exc)
                outcome.failed_pages += 1
                continue
            outcome.pages += 1
            if _apply(job, page):
                changed.add(job.duplicate_of or job.id)
    outcome.requests = budget.used
    outcome.changed = sorted(changed)
    return outcome
