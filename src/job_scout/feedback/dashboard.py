"""The dashboard (the root of the feedback page's server): every scored job in one table per
digest section, best first, with your vote and whether you applied to the company.

The digest's sections without its watchlist section: a watchlist job is sorted like any
other, and the watchlist is a column. Already-reported jobs are included. A job's details
are loaded when its row is opened (`/details/<job_id>`), not with the page.

Your filters live in a cookie, so the page arrives filtered: with thousands of rows, a page
filtered by its script would show every row until the script ran.
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
}


def filters_cookie(base_url: str | None) -> str:
    """The cookie's name: cookies ignore the port, so two instances served from one host
    would share one cookie; each instance's base URL differs."""
    return "dashboard_filters_" + hashlib.sha256((base_url or "").encode()).hexdigest()[:8]


@dataclass(frozen=True)
class Filters:
    """Each is "" (any) or one of its FILTER_VALUES."""

    watchlist: str = ""
    feedback: str = ""
    applied: str = ""

    @classmethod
    def from_cookies(cls, header: str, name: str) -> Filters:
        """The filters in the Cookie header's `name` (the script's URL-encoded query
        string); unknown or missing values mean "any"."""
        for part in header.split(";"):
            key, _, value = part.strip().partition("=")
            if key == name:
                query = parse_qs(unquote(value))
                chosen = {
                    field: given
                    for field, allowed in FILTER_VALUES.items()
                    if (given := (query.get(field) or [""])[0]) in allowed
                }
                return cls(**chosen)
        return cls()

    def any(self) -> bool:
        return bool(self.watchlist or self.feedback or self.applied)

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
        return not self.applied or (row.application is not None) == (self.applied == "yes")


@dataclass(frozen=True)
class DashboardRow:
    item: ReportItem
    signature: str  # the job's feedback signature: what the vote and applied forms send
    vote: Feedback | None
    application: Application | None
    application_key: ApplicationKey
    visible: bool = True  # matches your filters

    @property
    def search_text(self) -> str:
        company = self.item.group.canonical.company or ""
        return f"{self.item.title} {company}".lower()


@dataclass(frozen=True)
class DashboardSection:
    title: str
    rows: list[DashboardRow]
    open: bool
    note: str | None = None

    @property
    def shown(self) -> int:
        return sum(row.visible for row in self.rows)


def dashboard_sections(
    session: Session, config: AppConfig, secret: SecretStr, filters: Filters | None = None
) -> list[DashboardSection]:
    filters = filters or Filters()
    items = scored_items(session, watchlist_names(config))
    sections = build_sections(
        items, config.settings.digest, include_below=True, watchlist_section=False
    )
    voted = votes(session)
    applied = applications(session)

    def rows(section: list[ReportItem]) -> list[DashboardRow]:
        out = []
        for item in section:
            key = ApplicationKey.of(item.group.canonical)
            row = DashboardRow(
                item=item,
                signature=sign(secret, item.job_id),
                vote=voted.get(item.job_id),
                application=applied.get(key),
                application_key=key,
            )
            out.append(row if filters.matches(row) else replace(row, visible=False))
        return out

    return [
        DashboardSection("Top matches", rows(sections.top), open=True),
        DashboardSection(
            "Unclear location", rows(sections.unclear), open=True, note="worth a manual look"
        ),
        DashboardSection("Below threshold", rows(sections.below), open=False),
    ]
