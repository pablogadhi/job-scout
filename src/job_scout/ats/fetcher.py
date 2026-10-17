"""The company-board source (remoteintech, or company_boards without the seed): poll every
known company board.

The runner hands over the boards (ats_companies with status ok: seed, `companies` and
watchlist alike) and stores each board's outcome. One failing board is recorded and skipped;
the source fails only if every board failed.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

from job_scout.ats.platforms import PLATFORMS, Board, Platform
from job_scout.fetch.base import FetchContext, collect
from job_scout.fetch.http import FetchError
from job_scout.fetch.models import Posting

log = logging.getLogger(__name__)


class AtsFetcher:
    def fetch(self, ctx: FetchContext) -> list[Posting]:
        postings: list[Posting] = []
        for board in ctx.boards:
            platform = PLATFORMS[board.ats]
            try:
                items = platform.read(ctx, board.api_url)
            except (FetchError, ValueError) as exc:
                ctx.board_results[board.slug] = str(exc)
                log.warning(
                    "%s: board %s (%s) failed: %s", ctx.source.id, board.slug, board.ats, exc
                )
                continue
            if platform.details:
                items = [_with_details(ctx, platform, board, item) for item in items]
            postings.extend(
                collect(ctx, items, lambda item, p=platform, b=board: p.map_job(item, b))
            )
            ctx.board_results[board.slug] = None
        failed = sum(error is not None for error in ctx.board_results.values())
        if ctx.boards and failed == len(ctx.boards):
            raise FetchError(f"all {failed} company boards failed")
        return postings


def _with_details(ctx: FetchContext, platform: Platform, board: Board, item: Any) -> Any:
    """Details cost a request per job, so only for a job not stored yet whose title passes
    the title rules. The others keep the list's fields: a stored job is only marked seen, and
    a dropped title never reaches Claude. A failed request keeps the list's fields too."""
    if platform.details is None:
        return item
    try:
        listed = platform.map_job(item, board)
    except (ValidationError, KeyError, TypeError, ValueError, AttributeError):
        return item  # collect() logs and skips it
    if listed is None or listed.source_job_id in ctx.known_ids or not ctx.title_ok(listed.title):
        return item
    try:
        return platform.details(ctx, board, item)
    except (FetchError, ValueError) as exc:
        log.warning("%s: no details for %s: %s", ctx.source.id, listed.canonical_url, exc)
        return item
