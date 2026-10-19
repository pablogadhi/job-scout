"""Generic fetchers: one per method type. Sources plug in small mapper functions."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

import feedparser
import httpx
from pydantic import ValidationError

from job_scout.config.sources import SourceConfig
from job_scout.fetch.http import FetchError, HttpClient, RequestBudget
from job_scout.fetch.models import Posting
from job_scout.fetch.normalize import (
    html_to_text,
    parse_salary,
    struct_time_to_utc,
)

if TYPE_CHECKING:
    from job_scout.ats.platforms import Board

log = logging.getLogger(__name__)


def _any_title(_title: str) -> bool:
    return True


@dataclass
class FetchContext:
    source: SourceConfig
    http: HttpClient
    budget: RequestBudget
    now: datetime
    last_success_at: datetime | None = None
    environ: Mapping[str, str] = field(default_factory=dict)
    skipped_items: int = 0
    # Company-board sources only (ats_seed, ats_boards): the boards to poll, and each one's
    # outcome (slug -> None for success, else the error), which the runner stores.
    boards: list[Board] = field(default_factory=list)
    board_results: dict[str, str | None] = field(default_factory=dict)
    # For platforms that pay a request per job for its details (Workday): this source's jobs
    # already stored with a description, and the title rules of filters.yaml. A job stored
    # without one gets its details once its title passes (the store fills them in). For a
    # feed with a page step (`RssFetcher.details`): every job of the source already stored.
    known_ids: frozenset[str] = frozenset()
    title_ok: Callable[[str], bool] = _any_title

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.http.get(url, source=self.source, budget=self.budget, **kwargs)

    def get_json(self, url: str, **kwargs: Any) -> Any:
        return _json(self.get(url, **kwargs), url)

    def post_json(self, url: str, body: Any) -> Any:
        return _json(self.http.post(url, source=self.source, budget=self.budget, json=body), url)


def _json(response: httpx.Response, url: str) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise FetchError(f"invalid JSON from {url}: {exc}") from exc


class Fetcher(Protocol):
    def fetch(self, ctx: FetchContext) -> list[Posting]: ...


def collect(
    ctx: FetchContext, items: Iterable[Any], mapper: Callable[[Any], Posting | None]
) -> Iterator[Posting]:
    """Map items one by one. A malformed item is logged and skipped, not fatal to the source."""
    for item in items:
        try:
            posting = mapper(item)
        except (ValidationError, KeyError, TypeError, ValueError, AttributeError) as exc:
            ctx.skipped_items += 1
            log.warning("%s: skipped an item that didn't map: %s", ctx.source.id, exc)
            continue
        if posting is not None:
            yield posting


# --- RSS ---------------------------------------------------------------------------------

FeedEntry = Any  # feedparser.FeedParserDict


def entry_raw(entry: FeedEntry) -> dict[str, Any]:
    """JSON-safe copy of a feed entry (struct_time fields dropped; their strings remain)."""
    clean = {k: v for k, v in entry.items() if not k.endswith("_parsed")}
    return json.loads(json.dumps(clean, default=str))


def entry_html(entry: FeedEntry) -> str | None:
    if entry.get("content"):
        return entry["content"][0].get("value")
    return entry.get("summary")


_SALARY_LINE = re.compile(
    r"^(?:salary|compensation|pay|pay range|base pay|rate|salario)\b"
    r"[^:\n]{0,30}[:\-\u2013]?\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)


def labelled_salary(text: str | None) -> Any:
    """Parse salary only from lines labelled as salary, never from arbitrary description text."""
    if not text:
        return None
    for match in _SALARY_LINE.finditer(text):
        if salary := parse_salary(match.group(0)):
            return salary
    return None


def rss_posting(
    entry: FeedEntry,
    *,
    title: str,
    company: str | None,
    extra_tags: Iterable[str | None] = (),
    **overrides: Any,
) -> Posting:
    """Fields every feed has; `overrides` replace any of them."""
    values: dict[str, Any] = {
        "source_job_id": entry.get("id") or entry["link"],
        "canonical_url": entry["link"],
        "title": title,
        "company": company,
        "description_text": html_to_text(entry_html(entry)),
        "tags": [t["term"] for t in entry.get("tags", []) if t.get("term")],
        "posted_at": struct_time_to_utc(
            entry.get("published_parsed") or entry.get("updated_parsed")
        ),
        "raw": entry_raw(entry),
        **overrides,
    }
    values["tags"] = [*values["tags"], *(t for t in extra_tags if t)]
    posting = Posting(**values)
    if posting.salary_max is None:
        posting = posting.with_salary(labelled_salary(posting.description_text))
    return posting


Details = Callable[[FetchContext, Posting], Posting]


def with_details(ctx: FetchContext, postings: list[Posting], details: Details) -> list[Posting]:
    """Each new posting whose title passes filters.yaml, completed from its job page (one
    request each, also when two feeds list it). A page that fails keeps the feed's posting."""
    read: dict[str, Posting] = {}
    done: list[Posting] = []
    for posting in postings:
        job_id = posting.source_job_id
        if job_id in read:
            posting = read[job_id]
        elif job_id not in ctx.known_ids and ctx.title_ok(posting.title):
            try:
                posting = read[job_id] = details(ctx, posting)
            except FetchError as exc:
                log.warning("%s: no job page for %s: %s", ctx.source.id, posting.canonical_url, exc)
        done.append(posting)
    return done


@dataclass
class RssFetcher:
    """`shared` gives the fields every item of one endpoint has, read from its URL (e.g. a
    feed filtered to remote jobs whose items don't say so themselves). `details` completes a
    posting from its job page, for feeds that carry only a summary."""

    map_entry: Callable[[FeedEntry], Posting | None]
    shared: Callable[[str], dict[str, Any]] | None = None
    details: Details | None = None

    def fetch(self, ctx: FetchContext) -> list[Posting]:
        postings: list[Posting] = []
        for url in ctx.source.endpoints:
            feed = feedparser.parse(ctx.get(url).content)
            if not feed.entries and feed.get("bozo"):
                raise FetchError(f"not a valid feed: {url} ({feed.get('bozo_exception')})")
            shared = self.shared(url) if self.shared else {}
            postings.extend(
                p.model_copy(update=shared) for p in collect(ctx, feed.entries, self.map_entry)
            )
        return with_details(ctx, postings, self.details) if self.details else postings


# --- JSON --------------------------------------------------------------------------------


@dataclass
class JsonFetcher:
    """GET each endpoint, pull the item list out of the payload, map each item.

    `pages` yields the payloads for one endpoint; the default is a single GET. `keep` decides
    from the source's config which items to map (e.g. its `categories`).
    """

    items: Callable[[Any], Iterable[Any]]
    map_item: Callable[[Any], Posting | None]
    pages: Callable[[FetchContext, str], Iterator[Any]] | None = None
    keep: Callable[[FetchContext, Any], bool] | None = None

    def fetch(self, ctx: FetchContext) -> list[Posting]:
        def mapper(item: Any) -> Posting | None:
            # Inside collect(), so an item `keep` can't read is skipped like any other.
            if self.keep and not self.keep(ctx, item):
                return None
            return self.map_item(item)

        postings: list[Posting] = []
        for url in ctx.source.endpoints:
            payloads = self.pages(ctx, url) if self.pages else iter([ctx.get_json(url)])
            for payload in payloads:
                postings.extend(collect(ctx, self.items(payload), mapper))
        return postings
