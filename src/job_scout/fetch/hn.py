"""Hacker News "Ask HN: Who is hiring?" via the Algolia API.

Step 1 finds the newest monthly story; step 2 fetches its item tree. Each top-level comment
is one posting, keyed by comment id, so new comments arriving during the month are picked up
and old ones dedupe. Comments are free text; the first line is the "Company | Role | ..."
header, used as title and location. Claude parses the rest. Duplicates are matched on the
header's role alone, so the same job from a board merges with the comment.
"""

from __future__ import annotations

import re
from typing import Any

from job_scout.fetch.base import FetchContext, collect
from job_scout.fetch.http import FetchError
from job_scout.fetch.models import Posting
from job_scout.fetch.normalize import html_to_text, iso_to_utc, parse_salary

STORY_PREFIX = "Ask HN: Who is hiring?"
TITLE_MAX = 300
# Header segments that aren't the role: links, work arrangements, employment types, pay.
_NOT_ROLE = re.compile(
    r"https?://|www\.|\.(?:com|io|ai|org|net|dev|co|app)\b|[$€£]\s?\d|\d\s?k\b"
    r"|\b(?:remote|onsite|on-site|hybrid|full[- ]?time|part[- ]?time|contract|freelance"
    r"|intern(?:ship)?|visa|equity|salary|relocation)\b",
    re.I,
)


def _header(text: str) -> str:
    return text.split("\n", 1)[0].strip()


def _segments(header: str) -> list[str]:
    """'Acme | Senior SRE | REMOTE' or 'Acme (Remote US) — Senior SRE — $150k'."""
    for separator in ("|", " \u2014 ", " \u2013 "):
        parts = [p.strip() for p in header.split(separator)]
        if len(parts) > 1:
            return parts
    return [header]


def _role(segments: list[str]) -> str | None:
    """The first segment after the company that isn't a link, arrangement or pay. A place
    can slip through ("Munich, Germany"); it then matches no other job's title, which only
    means no merge."""
    return next(
        (s for s in segments[1:] if s and not _NOT_ROLE.search(re.sub(r"\([^)]*\)", "", s))),
        None,
    )


def map_comment(comment: dict[str, Any]) -> Posting | None:
    text = html_to_text(comment.get("text"))
    if not text or not comment.get("author"):
        return None  # deleted or dead
    header = _header(text)
    segments = _segments(header)
    return Posting(
        source_job_id=str(comment["id"]),
        canonical_url=f"https://news.ycombinator.com/item?id={comment['id']}",
        title=header[:TITLE_MAX],
        company=segments[0][:255] if len(segments) > 1 else None,
        location_raw=header,
        remote_raw=header,
        description_text=text,
        posted_at=iso_to_utc(comment.get("created_at")),
        raw={k: v for k, v in comment.items() if k != "children"},
        dedupe_title=_role(segments) if len(segments) > 1 else None,
    ).with_salary(next((s for seg in segments if (s := parse_salary(seg))), None))


class HackerNewsFetcher:
    def fetch(self, ctx: FetchContext) -> list[Posting]:
        search_url, item_url = ctx.source.endpoints
        hits = ctx.get_json(search_url).get("hits", [])
        story = next((h for h in hits if (h.get("title") or "").startswith(STORY_PREFIX)), None)
        if story is None:
            raise FetchError(f"no '{STORY_PREFIX}' story in the latest whoishiring posts")
        tree = ctx.get_json(item_url.format(story_id=story["objectID"]))
        return list(collect(ctx, tree.get("children", []), map_comment))
