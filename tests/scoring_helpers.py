"""Shared builders for scoring tests (synthetic data only)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from job_scout.db.models import Backend, Job, PrefilterResult, Source
from job_scout.scoring.backends.base import CallRequest, CallResult

T0 = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def result_for(job_id: int, score: int = 70, **overrides: Any) -> dict[str, Any]:
    """A valid ScoreResult payload item."""
    return {
        "job_id": job_id,
        "disqualified": False,
        "disqualify_reason": None,
        "remote": "yes",
        "location_eligibility": "pass",
        "location_notes": "Open to LATAM.",
        "salary": "unlisted",
        "salary_usd_range": None,
        "salary_notes": "",
        "role_category": "backend",
        "seniority": "Senior",
        "skill_fit": 4,
        "seniority_fit": 5,
        "matched_skills": ["Python", "Django"],
        "gaps": [],
        "four_day_week": "unknown",
        "wlb_positive": [],
        "wlb_negative": [],
        "employment_type": "unknown",
        "red_flags": [],
        "overall_score": score,
        "summary": "Synthetic posting that fits.",
        **overrides,
    }


def seed_jobs(
    sf: sessionmaker[Session],
    count: int,
    *,
    source: str = "board",
    start: int = 1,
    passed: bool = True,
) -> list[int]:
    """Canonical jobs with a pre-filter verdict; newer jobs have higher numbers."""
    ids = []
    with sf.begin() as session:
        if session.get(Source, source) is None:
            session.add(Source(id=source))
            session.flush()
        for n in range(start, start + count):
            job = Job(
                source_id=source,
                source_job_id=f"{source}-{n}",
                canonical_url=f"https://board.example/{source}/{n}",
                company=f"Company {n}",
                company_normalized=f"company {n}",
                title=f"Senior Backend Engineer {n}",
                title_normalized=f"senior backend engineer {n}",
                location_raw="Latin America",
                description_text=f"Synthetic description {n}. Python and Django.",
                posted_at=T0 + timedelta(hours=n),
                first_seen_at=T0,
                last_seen_at=T0,
                raw={},
                dedupe_key=f"{source}-{n}",
            )
            session.add(job)
            session.flush()
            session.add(PrefilterResult(job_id=job.id, passed=passed, reason="ok", fingerprint="x"))
            ids.append(job.id)
    return ids


class FakeScorer:
    """Scores every posting it is given, unless `behave` says otherwise."""

    def __init__(
        self,
        behave: Callable[[CallRequest], CallResult] | None = None,
        backend: Backend = Backend.CLI,
        model: str = "claude-haiku-5-5",
    ) -> None:
        self.backend = backend
        self.model = model
        self.behave = behave or ok_all
        self.requests: list[CallRequest] = []

    def score(self, request: CallRequest) -> CallResult:
        self.requests.append(request)
        return self.behave(request)


def ok_all(request: CallRequest, cost: float = 0.01) -> CallResult:
    return CallResult(
        "ok",
        payload={"results": [result_for(i) for i in request.job_ids]},
        input_tokens=1000,
        output_tokens=450 * len(request.job_ids),
        cost_usd=cost,
    )


def call_sizes(scorer: FakeScorer) -> list[int]:
    return [len(r.job_ids) for r in scorer.requests]


def ids_of(requests: Sequence[CallRequest]) -> list[tuple[int, ...]]:
    return [r.job_ids for r in requests]
