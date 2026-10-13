"""Keep `ats_companies` current: download the seed, apply the watchlist, detect boards.

`ats detect` runs it in full; `run` calls it with a cap (settings.ats) so the daily run stays
short. Each company's detection is committed on its own: an interrupted run loses nothing.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from job_scout.ats.detect import Detector
from job_scout.ats.seed import keep, parse_seed
from job_scout.ats.store import (
    SeedChanges,
    apply_watchlist,
    as_seed_company,
    last_seed_download,
    needing_detection,
    save_detection,
    upsert_seed,
)
from job_scout.config import AppConfig, SourceConfig
from job_scout.db.models import AtsCompany
from job_scout.db.types import utcnow
from job_scout.fetch.http import FetchError, HttpClient, RequestBudget

log = logging.getLogger(__name__)


@dataclass
class RefreshReport:
    seed_downloaded: bool = False
    seed_total: int = 0  # companies in the seed
    seed_kept: int = 0  # after the region / remote-policy filter
    changes: SeedChanges = field(default_factory=SeedChanges)
    watchlist: int = 0
    detected: Counter[str] = field(default_factory=Counter)  # ats or "unknown" -> companies
    remaining: int = 0  # still needing detection (capped runs)
    requests: int = 0


def _row(session: Session, slug: str) -> AtsCompany:
    return session.scalars(select(AtsCompany).where(AtsCompany.slug == slug)).one()


def seed_source(config: AppConfig) -> SourceConfig | None:
    return next((s for s in config.sources.sources if s.method == "ats_seed"), None)


def seed_due(session: Session, config: AppConfig, now: datetime) -> bool:
    last = last_seed_download(session)
    return last is None or now - last >= timedelta(days=config.settings.ats.seed_refresh_days)


def refresh(
    config: AppConfig,
    session_factory: sessionmaker[Session],
    http: HttpClient,
    *,
    download_seed: bool | None = None,
    limit: int | None = None,
    everything: bool = False,
    now: datetime | None = None,
) -> RefreshReport:
    """download_seed: True/False, or None for "when due" (settings.ats.seed_refresh_days)."""
    now = now or utcnow()
    source = seed_source(config)
    if source is None:
        raise FetchError("no ats_seed source in sources.yaml")
    budget = RequestBudget()
    report = RefreshReport()

    with session_factory() as session:
        due = seed_due(session, config, now)
    if download_seed or (download_seed is None and due):
        if not source.endpoints:
            raise FetchError(f"{source.id}: no endpoint for the seed")
        tarball = http.get(source.endpoints[0], source=source, budget=budget).content
        companies = parse_seed(tarball)
        kept = keep(companies, source.seed_regions, source.seed_remote_policies)
        if not kept:
            raise FetchError(f"{source.id}: the seed has no companies after filtering")
        with session_factory.begin() as session:
            report.changes = upsert_seed(session, kept, now)
        report.seed_downloaded, report.seed_total, report.seed_kept = (
            True,
            len(companies),
            len(kept),
        )

    with session_factory.begin() as session:
        report.watchlist = apply_watchlist(session, config.sources.watchlist, now)

    detector = Detector(http, source, config.sources.ats_platforms, budget)
    redetect_days = config.settings.ats.redetect_days
    with session_factory() as session:
        slugs = [
            r.slug
            for r in needing_detection(session, now, redetect_days, limit, everything=everything)
        ]
    for slug in slugs:
        with session_factory() as session:
            company = as_seed_company(_row(session, slug))
        detection = detector.detect(company)  # network: outside any transaction
        with session_factory.begin() as session:
            save_detection(_row(session, slug), detection, utcnow())
        report.detected[detection.ats or "unknown"] += 1
        log.info(
            "ats: %s -> %s",
            slug,
            f"{detection.ats}:{detection.token} ({detection.method})"
            if detection.ats
            else "unknown",
        )
    with session_factory() as session:
        report.remaining = len(needing_detection(session, utcnow(), redetect_days))
    report.requests = budget.used
    return report
