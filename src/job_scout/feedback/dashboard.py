"""The dashboard (the root of the feedback page's server): every scored job in one table per
digest section, best first, with your vote and whether you applied to the company.

The digest's sections without its watchlist section: a watchlist job is sorted like any
other, and the watchlist is a column. Already-reported jobs are included. A job's details
are loaded when its row is opened (`/details/<job_id>`), not with the page.

Your filters and each section's sort live in a cookie, so the page arrives filtered and
sorted: with thousands of rows, a page filtered by its script would show every row until the
script ran.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from urllib.parse import parse_qs, unquote

from pydantic import SecretStr
from sqlalchemy.orm import Session

from job_scout.config import AppConfig
from job_scout.db.models import Application, Feedback, Vote
from job_scout.feedback.links import sign
from job_scout.feedback.store import ApplicationKey, applications, votes
from job_scout.report.data import ReportItem, build_sections, scored_items, watchlist_names

NO_VOTE = "none"
_YES_NO = ("yes", "no")
FILTER_VALUES: dict[str, tuple[str, ...]] = {
    "watchlist": _YES_NO,
    "feedback": (*(vote.value for vote in Vote), NO_VOTE),
    "applied": _YES_NO,
    "available": _YES_NO,
}
# A filter's value until you choose one ("" is any): jobs marked no longer available are
# hidden unless you ask for them.
FILTER_DEFAULTS: dict[str, str] = {"available": "yes"}

# Each section's order: by score (best first, the digest's order) or by posting date.
SCORE, POSTED_DESC, POSTED_ASC = "score", "posted_desc", "posted_asc"
SORTS = (SCORE, POSTED_DESC, POSTED_ASC)
SECTION_KEYS = ("top", "unclear", "below")


def filters_cookie(base_url: str | None) -> str:
    """The cookie's name: cookies ignore the port, so two instances served from one host
    would share one cookie; each instance's base URL differs."""
    return "dashboard_filters_" + hashlib.sha256((base_url or "").encode()).hexdigest()[:8]


def cookie_state(header: str, name: str) -> dict[str, str]:
    """The Cookie header's `name` (the script's URL-encoded query string) as a dict; empty
    values are kept ("" is a filter set to any)."""
    for part in header.split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            query = parse_qs(unquote(value), keep_blank_values=True)
            return {k: v[0] for k, v in query.items()}
    return {}


@dataclass(frozen=True)
class Filters:
    """Each is "" (any) or one of its FILTER_VALUES."""

    watchlist: str = ""
    feedback: str = ""
    applied: str = ""
    available: str = FILTER_DEFAULTS["available"]

    @classmethod
    def from_state(cls, state: dict[str, str]) -> Filters:
        """The filters in the cookie's state; a missing one (no cookie yet, or one saved
        before that filter existed) or an unknown value gets its default."""
        chosen = {
            name: given
            for name, allowed in FILTER_VALUES.items()
            if (given := state.get(name)) is not None and (given == "" or given in allowed)
        }
        return cls(**chosen)

    def changed(self) -> bool:
        """Any filter differs from its default (what "Clear filters" goes back to)."""
        return any(self.value(name) != default(name) for name in FILTER_VALUES)

    def value(self, name: str) -> str:
        return getattr(self, name)

    def matches(self, row: DashboardRow) -> bool:
        if self.watchlist and row.item.watchlist != (self.watchlist == "yes"):
            return False
        vote = row.vote.vote if row.vote else None
        if self.feedback == NO_VOTE and vote is not None:
            return False
        if self.feedback not in ("", NO_VOTE) and vote != Vote(self.feedback):
            return False
        gone = row.item.unavailable_at is not None
        if self.available and gone == (self.available == "yes"):
            return False
        return not self.applied or (row.application is not None) == (self.applied == "yes")


def default(name: str) -> str:
    """A filter's value until you choose one."""
    return FILTER_DEFAULTS.get(name, "")


def section_sorts(state: dict[str, str]) -> dict[str, str]:
    """Each section's sort (cookie keys `sort_<section>`); unknown or missing is by score."""
    return {
        key: given if (given := state.get(f"sort_{key}")) in SORTS else SCORE
        for key in SECTION_KEYS
    }


@dataclass(frozen=True)
class DashboardRow:
    item: ReportItem
    signature: str  # the job's feedback signature: what the vote and applied forms send
    vote: Feedback | None
    application: Application | None
    application_key: ApplicationKey
    rank: int  # position in score order, so the script can sort back to it
    visible: bool = True  # matches your filters

    @property
    def search_text(self) -> str:
        company = self.item.group.canonical.company or ""
        return f"{self.item.title} {company}".lower()


@dataclass(frozen=True)
class DashboardSection:
    key: str  # its name in the sort cookie
    title: str
    rows: list[DashboardRow]
    open: bool
    sort: str = SCORE
    note: str | None = None

    @property
    def shown(self) -> int:
        return sum(row.visible for row in self.rows)


def sort_rows(rows: list[DashboardRow], sort: str) -> list[DashboardRow]:
    """`rows` (in score order) by `sort`. By posting date, jobs without one go last either
    way and ties keep their score order."""
    if sort == SCORE:
        return rows
    dated = [(posted, row) for row in rows if (posted := row.item.posted_at) is not None]
    # Python's sort is stable, reversed too: equal dates stay in score order.
    dated.sort(key=lambda pair: pair[0], reverse=sort == POSTED_DESC)
    return [row for _, row in dated] + [row for row in rows if row.item.posted_at is None]


def dashboard_sections(
    session: Session,
    config: AppConfig,
    secret: SecretStr,
    filters: Filters | None = None,
    sorts: dict[str, str] | None = None,
) -> list[DashboardSection]:
    filters = filters or Filters()
    sorts = sorts or {}
    items = scored_items(session, watchlist_names(config))
    sections = build_sections(
        items, config.settings.digest, include_below=True, watchlist_section=False
    )
    voted = votes(session)
    applied = applications(session)

    def section(
        key: str, title: str, items: list[ReportItem], *, open: bool, note: str | None = None
    ) -> DashboardSection:
        out = []
        for rank, item in enumerate(items):
            app_key = ApplicationKey.of(item.group.canonical)
            row = DashboardRow(
                item=item,
                signature=sign(secret, item.job_id),
                vote=voted.get(item.job_id),
                application=applied.get(app_key),
                application_key=app_key,
                rank=rank,
            )
            out.append(row if filters.matches(row) else replace(row, visible=False))
        sort = sorts.get(key, SCORE)
        return DashboardSection(key, title, sort_rows(out, sort), open=open, sort=sort, note=note)

    return [
        section("top", "Top matches", sections.top, open=True),
        section(
            "unclear", "Unclear location", sections.unclear, open=True, note="worth a manual look"
        ),
        section("below", "Below threshold", sections.below, open=False),
    ]
