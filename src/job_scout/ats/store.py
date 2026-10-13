"""The `ats_companies` table: seed upserts, the watchlist, and what to detect or poll."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from job_scout.ats.detect import Detection
from job_scout.ats.platforms import Board, api_url
from job_scout.ats.seed import SeedCompany
from job_scout.config.sources import AtsPlatform, WatchlistEntry
from job_scout.db.models import AtsCompany, AtsMethod, AtsSeed, AtsStatus


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


@dataclass
class SeedChanges:
    added: int = 0
    updated: int = 0  # careers URL or website changed: re-detected
    dropped: int = 0  # no longer in the (filtered) seed: not polled


def upsert_seed(session: Session, companies: Iterable[SeedCompany], now: datetime) -> SeedChanges:
    changes = SeedChanges()
    rows = {r.slug: r for r in session.scalars(select(AtsCompany))}
    listed: set[str] = set()
    for company in companies:
        listed.add(company.slug)
        row = rows.get(company.slug)
        if row is None:
            row = AtsCompany(
                slug=company.slug,
                company=company.name,
                status=AtsStatus.PENDING,
                source=AtsSeed.REMOTEINTECH,
            )
            session.add(row)
            changes.added += 1
        elif row.source == AtsSeed.REMOTEINTECH and (
            (row.careers_url, row.website) != (company.careers_url, company.website)
            or row.status == AtsStatus.NOT_IN_SEED
        ):
            row.detected_at = None  # detect again
            if row.status == AtsStatus.NOT_IN_SEED:
                row.status = AtsStatus.PENDING
            changes.updated += 1
        row.website, row.careers_url = company.website, company.careers_url
        row.region, row.remote_policy = company.region, company.remote_policy
        if row.source == AtsSeed.REMOTEINTECH:
            row.company = company.name
        row.seen_in_seed_at = now
    for slug, row in rows.items():
        if (
            slug not in listed
            and row.source == AtsSeed.REMOTEINTECH
            and row.status != AtsStatus.NOT_IN_SEED
        ):
            row.status = AtsStatus.NOT_IN_SEED
            changes.dropped += 1
    return changes


def apply_watchlist(session: Session, entries: Iterable[WatchlistEntry], now: datetime) -> int:
    """Watchlist entries are polled whatever the seed or detection says. A company removed
    from the watchlist goes back to the seed's detection (or stops being polled)."""
    entries = list(entries)
    current = {slugify(e.company) for e in entries}
    for row in session.scalars(select(AtsCompany).where(AtsCompany.source == AtsSeed.WATCHLIST)):
        if row.slug not in current:
            row.source = AtsSeed.REMOTEINTECH
            row.method, row.detected_at = None, None
            row.status = AtsStatus.PENDING if row.seen_in_seed_at else AtsStatus.NOT_IN_SEED
    count = 0
    for entry in entries:
        slug = slugify(entry.company)
        row = session.scalars(select(AtsCompany).where(AtsCompany.slug == slug)).one_or_none()
        if row is None:
            row = AtsCompany(
                slug=slug, company=entry.company, status=AtsStatus.OK, source=AtsSeed.WATCHLIST
            )
            session.add(row)
        row.company = entry.company
        row.careers_url = entry.careers_url
        row.ats, row.token = entry.ats, entry.token
        row.method, row.status, row.source = AtsMethod.WATCHLIST, AtsStatus.OK, AtsSeed.WATCHLIST
        row.detected_at = row.detected_at or now
        count += 1
    return count


def needing_detection(
    session: Session,
    now: datetime,
    redetect_days: int,
    limit: int | None = None,
    *,
    everything: bool = False,
) -> list[AtsCompany]:
    """Seed companies never detected or detected too long ago (all of them with `everything`)."""
    query = select(AtsCompany).where(
        AtsCompany.source == AtsSeed.REMOTEINTECH, AtsCompany.status != AtsStatus.NOT_IN_SEED
    )
    if not everything:
        stale = now - timedelta(days=redetect_days)
        query = query.where(or_(AtsCompany.detected_at.is_(None), AtsCompany.detected_at < stale))
    query = query.order_by(
        AtsCompany.detected_at.is_not(None), AtsCompany.detected_at, AtsCompany.slug
    )
    if limit is not None:
        query = query.limit(limit)
    return list(session.scalars(query))


def save_detection(row: AtsCompany, detection: Detection, now: datetime) -> None:
    row.detected_at = now
    row.ats, row.token, row.method = detection.ats, detection.token, detection.method
    row.status = AtsStatus.OK if detection.ats else AtsStatus.UNKNOWN_ATS
    row.last_poll_error = None if detection.ats else detection.note


def as_seed_company(row: AtsCompany) -> SeedCompany:
    return SeedCompany(
        slug=row.slug,
        name=row.company,
        website=row.website,
        careers_url=row.careers_url,
        region=row.region,
        remote_policy=row.remote_policy,
    )


def boards(session: Session, platforms: Mapping[str, AtsPlatform]) -> list[Board]:
    """Every board to poll: status ok, on a configured platform."""
    rows = session.scalars(
        select(AtsCompany)
        .where(AtsCompany.status == AtsStatus.OK, AtsCompany.ats.in_(list(platforms)))
        .order_by(AtsCompany.ats, AtsCompany.slug)
    )
    return [
        Board(r.slug, r.company, r.ats, r.token, api_url(platforms[r.ats].api, r.token))
        for r in rows
        if r.ats and r.token
    ]


def last_seed_download(session: Session) -> datetime | None:
    return session.scalar(select(func.max(AtsCompany.seen_in_seed_at)))


def record_polls(session: Session, results: Mapping[str, str | None], now: datetime) -> None:
    """Per-board poll outcome from the fetcher: None for success, else the error."""
    if not results:
        return
    for row in session.scalars(select(AtsCompany).where(AtsCompany.slug.in_(list(results)))):
        row.last_polled_at = now
        row.last_poll_error = results[row.slug]


def counts(session: Session) -> dict[tuple[str, str], int]:
    """(status, ats or '-') -> companies."""
    rows = session.execute(
        select(AtsCompany.status, AtsCompany.ats, func.count()).group_by(
            AtsCompany.status, AtsCompany.ats
        )
    )
    return {(str(status), ats or "-"): n for status, ats, n in rows}
