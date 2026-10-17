"""`job-scout export`: everything as CSV for skimming, and an optional SQLite snapshot.

CSV: one row per stored posting (duplicates included, linked by `group_id`), with its
group's pre-filter verdict and the score under the current profile. Fields a profile adds to
its output format (beyond the ones the reports name) get a column each, after the fixed ones.
Snapshot: SQLite's online backup API, consistent even while the pipeline writes (copying the
.db file mid-write can give a broken or stale database).
"""

from __future__ import annotations

import csv
import sqlite3
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from job_scout.db.models import Job, PrefilterResult, Score
from job_scout.report.data import field_text

COLUMNS = [
    "job_id", "group_id", "duplicate", "source", "title", "company", "location", "remote",
    "salary_min", "salary_max", "salary_currency", "salary_period", "posted_at",
    "first_seen_at", "url", "apply_url", "prefilter", "prefilter_reason", "score_status",
    "score", "disqualified", "disqualify_reason", "location_eligibility", "summary",
    "scored_at", "backend", "model",
]  # fmt: skip

# A cell starting with one of these runs as a formula in a spreadsheet; posting text is
# untrusted, so such cells get a leading apostrophe.
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if hasattr(value, "value"):  # enums
        value = value.value
    if isinstance(value, str) and value.startswith(_FORMULA_START):
        return "'" + value
    return value


def export_csv(session: Session, path: Path, extra_fields: Sequence[str] = ()) -> int:
    """Write the CSV, with a column per name in `extra_fields`; return the number of rows."""
    verdicts = {r.job_id: r for r in session.scalars(select(PrefilterResult))}
    scores = {s.job_id: s for s in session.scalars(select(Score))}
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = 0
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([*COLUMNS, *extra_fields])
        for job in session.scalars(select(Job).order_by(Job.id)).yield_per(500):
            group_id = job.duplicate_of or job.id
            verdict = verdicts.get(group_id)
            score = scores.get(group_id)
            result = (score.result or {}) if score else {}
            writer.writerow(
                _cell(v)
                for v in (
                    job.id,
                    group_id,
                    job.duplicate_of is not None,
                    job.source_id,
                    job.title,
                    job.company,
                    job.location_raw,
                    job.remote_raw,
                    job.salary_min,
                    job.salary_max,
                    job.salary_currency,
                    job.salary_period,
                    job.posted_at,
                    job.first_seen_at,
                    job.canonical_url,
                    job.apply_url,
                    "" if verdict is None else ("passed" if verdict.passed else "dropped"),
                    verdict.reason if verdict else None,
                    score.status if score else None,
                    score.overall_score if score else None,
                    score.disqualified if score else None,
                    result.get("disqualify_reason"),
                    result.get("location_eligibility"),
                    result.get("summary"),
                    score.scored_at if score else None,
                    score.backend if score else None,
                    score.model if score else None,
                    *(field_text(result.get(name)) for name in extra_fields),
                )
            )
            rows += 1
    return rows


def snapshot_sqlite(db_url: str, path: Path) -> Path:
    url = make_url(db_url)
    if url.get_backend_name() != "sqlite" or not url.database:
        raise ValueError(f"--sqlite needs a SQLite database file, not {url.get_backend_name()}")
    path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{url.database}?mode=ro", uri=True)
    target = sqlite3.connect(path)
    try:
        with target:
            source.backup(target)
    finally:
        source.close()
        target.close()
    return path
