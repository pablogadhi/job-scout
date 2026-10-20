"""Render sections to HTML and plain text, and write report files."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape

from job_scout.config import AppConfig
from job_scout.feedback.links import FeedbackLinks
from job_scout.report.data import Sections, SourceHealth, by_company, item_mark
from job_scout.scoring.points import fingerprint
from job_scout.scoring.status import ScoringStatus

_ENV = Environment(
    loader=PackageLoader("job_scout.report", "templates"),
    autoescape=select_autoescape(enabled_extensions=("html",), default_for_string=True),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)
# The plain-text twin places its newlines itself ({%- ... %} where a line must not start):
# trimming after block tags would glue its lines together.
_TEXT_ENV = Environment(
    loader=PackageLoader("job_scout.report", "templates"),
    autoescape=False,
    undefined=StrictUndefined,
    keep_trailing_newline=True,
)


def http_url(url: str | None) -> str:
    """Only http(s) links reach a report: a posting can't smuggle in `javascript:` URLs."""
    if url and urlsplit(url.strip()).scheme.lower() in ("http", "https"):
        return url.strip()
    return "#"


_ENV.filters["http_url"] = http_url
_ENV.filters["mark"] = _TEXT_ENV.filters["mark"] = item_mark
_ENV.filters["by_company"] = _TEXT_ENV.filters["by_company"] = by_company


@dataclass(frozen=True)
class Rendered:
    subject: str
    html: str
    text: str


def render(
    config: AppConfig,
    *,
    heading: str,
    sections: Sections,
    status: ScoringStatus,
    health: list[SourceHealth],
    now: datetime,
    since: datetime | None = None,
    include_below: bool = False,
    feedback: FeedbackLinks | None = None,
) -> Rendered:
    tz = ZoneInfo(config.settings.schedule.timezone)
    context = {
        "heading": heading,
        "sections": sections,
        "status": status,
        "health": [_local(h, tz) for h in health],
        "settings": config.settings.digest,
        "profile_hash": config.profile.sha256,
        "scoring_fingerprint": fingerprint(config.points),
        "generated_at": now.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z"),
        "since": since.astimezone(tz).strftime("%Y-%m-%d %H:%M") if since else None,
        "include_below": include_below,
        "feedback": feedback,
    }
    counts = f"{len(sections.top)} top, {len(sections.unclear)} unclear"
    if sections.watchlist:
        counts = f"{len(sections.watchlist)} watchlist, {counts}"
    return Rendered(
        subject=f"{heading}: {counts}",
        html=_ENV.get_template("report.html").render(context),
        text=_TEXT_ENV.get_template("report.txt").render(context),
    )


def _local(health: SourceHealth, tz: ZoneInfo) -> SourceHealth:
    if health.last_run_at is None:
        return health
    return SourceHealth(
        source_id=health.source_id,
        last_run_at=health.last_run_at.astimezone(tz),
        last_item_count=health.last_item_count,
        consecutive_failures=health.consecutive_failures,
        consecutive_zero_runs=health.consecutive_zero_runs,
        missing_env=health.missing_env,
    )


def write_report(directory: Path, stem: str, now: datetime, html: str) -> Path:
    """reports/<stem>-YYYYmmddTHHMMSSZ.html, plus reports/<stem>-latest.html as a copy."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stem}-{now:%Y%m%dT%H%M%SZ}.html"
    path.write_text(html, encoding="utf-8")
    (directory / f"{stem}-latest.html").write_text(html, encoding="utf-8")
    return path
