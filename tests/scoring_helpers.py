"""Shared builders for scoring tests (synthetic data only)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from job_scout.config import load_points
from job_scout.db.models import Backend, Job, PrefilterResult, Score, Source
from job_scout.scoring.backends.base import CallRequest, CallResult
from job_scout.scoring.points import apply, compute, fingerprint

T0 = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
TEST_TABLE = load_points(Path(__file__).parent / "fixtures" / "config" / "scoring.yaml")


# Positive signals without a badge, largest first: they top up a score exactly.
_FILLERS = [
    ("async_first", 4), ("flexible_hours", 3), ("pto_minimum", 3), ("after_hours_limits", 3),
    ("sustainable_pace", 3), ("sabbatical", 2), ("remote_handbook", 2),
    ("domain_fintech", 2), ("domain_healthcare", 2), ("domain_crm", 1),
    ("domain_b2b_platforms", 1),
]  # fmt: skip


def result_for(job_id: int, score: int = 70, **overrides: Any) -> dict[str, Any]:
    """A valid reply item whose facts compute to `score` under the pinned scoring.yaml
    (tests/fixtures/config): a senior backend role, eligible, salary unlisted, unless
    overridden; requirements and signals are filled in to reach the score."""
    result: dict[str, Any] = {
        "job_id": job_id,
        "remote": "yes",
        "location_eligibility": "pass",
        "location_notes": "Open to LATAM.",
        "salary": "unlisted",
        "salary_usd_range": None,
        "salary_notes": "",
        "language": "english-or-spanish",
        "role_category": "backend",
        "seniority": "Senior",
        "seniority_level": "senior",
        "requirements": [],
        "signals": [],
        "employment_type": "unknown",
        "red_flags": [],
        "summary": "Synthetic posting that fits.",
        **overrides,
    }
    if "requirements" not in overrides and "signals" not in overrides:
        result["requirements"], result["signals"] = _facts_for(score, result, overrides)
    return result


def with_points(score: Score, result: dict[str, Any]) -> Score:
    """`score` holding `result`, with the points the pinned scoring.yaml gives it."""
    breakdown = compute(TEST_TABLE, result)
    assert breakdown is not None
    score.result = result
    apply(score, breakdown, fingerprint(TEST_TABLE))
    return score


def _fixed_points(result: dict[str, Any]) -> int:
    breakdown = compute(TEST_TABLE, result)
    assert breakdown is not None
    return sum(p.points for p in breakdown.parts if p.name not in ("skills", "signals"))


def _facts_for(
    score: int, result: dict[str, Any], overrides: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    need = score - _fixed_points(result)
    if need < 0:  # a weak job: no level, no target role (unless the test set them)
        if "seniority_level" not in overrides:
            result["seniority_level"] = "intern"
        if "role_category" not in overrides:
            result["role_category"] = "other"
        need = max(0, score - _fixed_points(result))
    # 8 required skills, 5 points each when matched; signals add the rest.
    core = min(need, 40) // 5
    requirements = [
        {
            "skill": f"Skill {n}",
            "importance": "required",
            "primary": False,
            "match": "core" if n < core else "missing",
            "evidence": f"Skill {n} experience",
        }
        for n in range(8)
    ]
    left, signals = need - core * 5, []
    for name, points in _FILLERS:
        if 0 < points <= left:
            signals.append({"signal": name, "evidence": f"quote for {name}"})
            left -= points
    return requirements, signals


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
