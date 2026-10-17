"""Batch-size calibration (PLAN.md §10 Phase 3): does packing postings into one call change
the scores? Scores the same sample with a reference configuration (api, 1 posting per call)
and with candidates (e.g. cli at 20, 40 and 60 per call). Configurations can also differ in
model and effort (`--configs`), to compare models. Nothing is written to `scores`.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, get_args

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from job_scout.config import AppConfig
from job_scout.config.points import PointsTable
from job_scout.config.settings import Effort
from job_scout.db.groups import load_groups
from job_scout.db.models import Backend, Job, PrefilterResult
from job_scout.db.types import utcnow
from job_scout.feedback.examples import select_examples
from job_scout.scoring.backends.base import CallRequest, Scorer
from job_scout.scoring.points import compute
from job_scout.scoring.prompt import PostingInput, render_posting, system_prompt, user_message
from job_scout.scoring.runner import max_output_tokens
from job_scout.scoring.schema import ScoreSchema, score_schema

_EFFORTS: dict[str, Effort] = {e: e for e in get_args(Effort)}


@dataclass(frozen=True)
class ConfigSpec:
    """'backend:model:postings_per_call[:effort]', e.g. 'api:claude-haiku-5-5:1:low'."""

    backend: Backend
    model: str
    size: int
    effort: Effort | None = None

    @classmethod
    def parse(cls, text: str) -> ConfigSpec:
        parts = text.strip().split(":")
        if len(parts) not in (3, 4) or parts[0] not in set(Backend) or not parts[2].isdigit():
            raise ValueError(f"{text!r}: expected backend:model:postings_per_call[:effort]")
        effort = _EFFORTS.get(parts[3]) if len(parts) == 4 else None
        if len(parts) == 4 and effort is None:
            raise ValueError(f"{text!r}: effort must be one of {', '.join(_EFFORTS)}")
        return cls(Backend(parts[0]), parts[1], int(parts[2]), effort)

    @property
    def name(self) -> str:
        effort = f" effort={self.effort}" if self.effort else ""
        return f"{self.backend} {self.model} x{self.size}{effort}"


@dataclass(frozen=True)
class Configuration:
    name: str  # e.g. "api x1", "cli x20"
    scorer: Scorer
    postings_per_call: int


@dataclass
class CallDetail:
    """One calibration call, saved in the report so failures can be inspected later."""

    postings: int
    status: str
    error: str | None
    returned: int | None  # results in the reply; None when it had no results list
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    seconds: float
    extra: dict[str, Any]
    valid: int = 0
    rejected: int = 0
    first_rejection: str | None = None
    unknown_ids: list[int] = field(default_factory=list)
    missing_ids: list[int] = field(default_factory=list)


@dataclass
class ConfigResult:
    name: str
    scores: dict[int, int] = field(default_factory=dict)  # computed with scoring.yaml
    summaries: dict[int, str] = field(default_factory=dict)
    results: dict[int, dict[str, Any]] = field(default_factory=dict)  # replies, to re-score
    calls: int = 0
    failed_calls: int = 0
    cost_usd: float = 0.0
    seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0  # includes thinking tokens
    calls_detail: list[CallDetail] = field(default_factory=list)


@dataclass
class Calibration:
    job_ids: list[int]
    titles: dict[int, str]
    companies: dict[int, str]
    reference: ConfigResult
    candidates: list[ConfigResult]
    # The profile's 'a | b' fields and lists of objects: how often two configurations
    # extracted the same facts, which is what the score is computed from.
    enum_fields: list[str] = field(default_factory=list)
    objects_fields: list[str] = field(default_factory=list)

    def diff_stats(self, candidate: ConfigResult) -> dict[str, Any]:
        pairs = [
            (self.reference.scores[j], candidate.scores[j])
            for j in self.job_ids
            if j in self.reference.scores and j in candidate.scores
        ]
        if not pairs:
            return {"compared": 0, "mean_abs_diff": None, "max_abs_diff": None, "within_10": 0}
        diffs = [abs(a - b) for a, b in pairs]
        both = [
            (self.reference.results[j], candidate.results[j])
            for j in self.job_ids
            if j in self.reference.results and j in candidate.results
        ]
        return {
            "compared": len(pairs),
            "missing": len(self.job_ids) - len(candidate.scores),
            "mean_abs_diff": round(sum(diffs) / len(diffs), 1),
            "max_abs_diff": max(diffs),
            "within_10": sum(d <= 10 for d in diffs),
            "rank_agreement": _rank_agreement(pairs),
            "same_value": {
                name: round(sum(a.get(name) == b.get(name) for a, b in both) / len(both), 3)
                for name in self.enum_fields
            }
            if both
            else {},
            "overlap": {
                name: round(sum(_overlap(a.get(name), b.get(name)) for a, b in both) / len(both), 3)
                for name in self.objects_fields
            }
            if both
            else {},
        }


def _overlap(a: Any, b: Any) -> float:
    """Jaccard overlap of two lists of objects, by each object's first field (the skill or
    signal named); 1.0 when both are empty."""

    def names(items: Any) -> set[str]:
        return {
            str(next(iter(i.values()))).strip().lower()
            for i in items or []
            if isinstance(i, dict) and i
        }

    x, y = names(a), names(b)
    return len(x & y) / len(x | y) if x | y else 1.0


def _rank_agreement(pairs: Sequence[tuple[int, int]]) -> float | None:
    """Share of job pairs ordered the same way by both configurations (ties skipped)."""
    agree = total = 0
    for i, (a1, b1) in enumerate(pairs):
        for a2, b2 in pairs[i + 1 :]:
            if a1 == a2 or b1 == b2:
                continue
            total += 1
            agree += (a1 > a2) == (b1 > b2)
    return round(agree / total, 3) if total else None


def sample_job_ids(session: Session, size: int, seed: int | None = None) -> list[int]:
    ids = list(
        session.scalars(
            select(Job.id)
            .join(PrefilterResult, PrefilterResult.job_id == Job.id)
            .where(Job.duplicate_of.is_(None), PrefilterResult.passed.is_(True))
            .order_by(Job.id)
        )
    )
    return sorted(random.Random(seed).sample(ids, min(size, len(ids))))


def _score_with(
    configuration: Configuration,
    system: str,
    scores: ScoreSchema,
    table: PointsTable,
    postings: Sequence[PostingInput],
) -> ConfigResult:
    result = ConfigResult(configuration.name)
    schema = scores.json_schema()
    started = time.monotonic()
    size = configuration.postings_per_call
    for start in range(0, len(postings), size):
        chunk = list(postings[start : start + size])
        call_started = time.monotonic()
        reply = configuration.scorer.score(
            CallRequest(
                system=system,
                user=user_message(chunk),
                job_ids=tuple(p.job_id for p in chunk),
                schema=schema,
                max_output_tokens=max_output_tokens(len(chunk)),
            )
        )
        result.calls += 1
        result.cost_usd += reply.cost_usd or 0.0
        result.input_tokens += reply.input_tokens or 0
        result.output_tokens += reply.output_tokens or 0
        items = (
            reply.payload.get("results")
            if reply.status == "ok" and isinstance(reply.payload, dict)
            else None
        )
        detail = CallDetail(
            postings=len(chunk),
            status=reply.status,
            error=reply.error,
            returned=len(items) if isinstance(items, list) else None,
            input_tokens=reply.input_tokens,
            output_tokens=reply.output_tokens,
            cost_usd=reply.cost_usd,
            seconds=round(time.monotonic() - call_started, 1),
            extra=reply.extra,
        )
        result.calls_detail.append(detail)
        if not isinstance(items, list):
            result.failed_calls += 1
            if reply.status == "usage_limit":
                break
            continue
        wanted = {p.job_id for p in chunk}
        for item in items:
            try:
                data, core = scores.parse(item)
            except ValidationError as exc:
                detail.rejected += 1
                detail.first_rejection = detail.first_rejection or str(exc)[:500]
                continue
            job_id = data["job_id"]
            breakdown = compute(table, data)
            if job_id not in wanted:
                detail.unknown_ids.append(job_id)
            elif job_id not in result.scores and breakdown is not None:
                detail.valid += 1
                result.scores[job_id] = breakdown.total
                result.summaries[job_id] = core.summary
                result.results[job_id] = data
        detail.missing_ids = sorted(wanted - result.scores.keys())
    result.seconds = round(time.monotonic() - started, 1)
    return result


def run_calibration(
    config: AppConfig,
    session: Session,
    job_ids: Sequence[int],
    reference: Configuration,
    candidates: Sequence[Configuration],
) -> Calibration:
    max_chars = config.settings.scoring.description_max_chars
    groups = load_groups(session, job_ids)
    postings = [render_posting(g, max_chars) for g in groups]
    examples = select_examples(
        session, config.settings.feedback.prompt_examples, config.settings.digest.min_score
    )
    system = system_prompt(config.profile, examples)  # the same prompt a real run sends
    scores = score_schema(config.profile)
    return Calibration(
        job_ids=[g.canonical.id for g in groups],
        titles={g.canonical.id: g.canonical.title for g in groups},
        companies={g.canonical.id: g.canonical.company or "" for g in groups},
        reference=_score_with(reference, system, scores, config.points, postings),
        candidates=[_score_with(c, system, scores, config.points, postings) for c in candidates],
        enum_fields=[f.name for f in config.profile.output_fields if f.kind == "enum"],
        objects_fields=[f.name for f in config.profile.output_fields if f.kind == "objects"],
    )


def save(calibration: Calibration, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"calibration-{utcnow():%Y%m%dT%H%M%SZ}.json"
    data = asdict(calibration)
    data["stats"] = {c.name: calibration.diff_stats(c) for c in calibration.candidates}
    path.write_text(json.dumps(data, indent=2, default=str) + "\n")
    return path


def estimate_calls(sample: int, sizes: Mapping[str, int]) -> dict[str, int]:
    return {name: -(-sample // size) for name, size in sizes.items()}
