"""The dashboard (`/d` on the feedback page's server): every scored job in one table per
digest section, best first, with your vote and whether you applied to the company.

The digest's sections without its watchlist section: a watchlist job is sorted like any
other, and the watchlist is a column. Already-reported jobs are included.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import SecretStr
from sqlalchemy.orm import Session

from job_scout.config import AppConfig
from job_scout.db.models import Application, Feedback
from job_scout.feedback.links import sign
from job_scout.feedback.store import ApplicationKey, applications, votes
from job_scout.report.data import ReportItem, build_sections, scored_items, watchlist_names


@dataclass(frozen=True)
class DashboardRow:
    item: ReportItem
    signature: str  # the job's feedback signature: what the vote and applied forms send
    vote: Feedback | None
    application: Application | None
    application_key: ApplicationKey

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


def dashboard_sections(
    session: Session, config: AppConfig, secret: SecretStr
) -> list[DashboardSection]:
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
            out.append(
                DashboardRow(
                    item=item,
                    signature=sign(secret, item.job_id),
                    vote=voted.get(item.job_id),
                    application=applied.get(key),
                    application_key=key,
                )
            )
        return out

    return [
        DashboardSection("Top matches", rows(sections.top), open=True),
        DashboardSection(
            "Unclear location", rows(sections.unclear), open=True, note="worth a manual look"
        ),
        DashboardSection("Below threshold", rows(sections.below), open=False),
    ]
