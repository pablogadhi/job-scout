"""The `remoteintech` source (method ats_seed): poll every known company board.

The runner hands over the boards (ats_companies with status ok, seed and watchlist alike)
and stores each board's outcome. One failing board is recorded and skipped; the source
fails only if every board failed.
"""

from __future__ import annotations

import logging

from job_scout.ats.platforms import PLATFORMS
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
                items = platform.jobs(ctx.get_json(board.api_url))
            except (FetchError, ValueError) as exc:
                ctx.board_results[board.slug] = str(exc)
                log.warning(
                    "%s: board %s (%s) failed: %s", ctx.source.id, board.slug, board.ats, exc
                )
                continue
            postings.extend(
                collect(ctx, items, lambda item, p=platform, b=board: p.map_job(item, b))
            )
            ctx.board_results[board.slug] = None
        failed = sum(error is not None for error in ctx.board_results.values())
        if ctx.boards and failed == len(ctx.boards):
            raise FetchError(f"all {failed} company boards failed")
        return postings
