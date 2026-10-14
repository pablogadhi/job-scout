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
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from job_scout.config import AppConfig
from job_scout.db.groups import load_groups
from job_scout.db.models import Job, PrefilterResult
from job_scout.db.types import utcnow
from job_scout.feedback.examples import select_examples
from job_scout.scoring.backends.base import CallRequest, Scorer
from job_scout.scoring.prompt import PostingInput, render_posting, system_prompt, user_message
from job_scout.scoring.runner import max_output_tokens
from job_scout.scoring.schema import ScoreResult, output_json_schema


@dataclass(frozen=True)
class ConfigSpec:
    """'backend:model:postings_per_call[:effort]', e.g. 'api:claude-haiku-5-5:1:low'."""

    backend: str
    model: str
    size: int
    effort: str | None = None

    @classmethod
    def parse(cls, text: str) -> ConfigSpec:
        parts = text.strip().split(":")
        if len(parts) not in (3, 4) or parts[0] not in ("api", "cli") or not parts[2].isdigit():
            raise ValueError(f"{text!r}: expected backend:model:postings_per_call[:effort]")
        return cls(parts[0], parts[1], int(parts[2]), parts[3] if len(parts) == 4 else None)

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
class ConfigResult:
    name: str
    scores: dict[int, int] = field(default_factory=dict)
    summaries: dict[int, str] = field(default_factory=dict)
    calls: int = 0
    failed_calls: int = 0
    cost_usd: float = 0.0
    seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0  # includes thinking tokens
    calls_detail: list[dict[str, Any]] = field(default_factory=list)  # one entry per call


@dataclass
class Calibration:
    job_ids: list[int]
    titles: dict[int, str]
    companies: dict[int, str]
    reference: ConfigResult
    candidates: list[ConfigResult]

    def diff_stats(self, candidate: ConfigResult) -> dict[str, float | int | None]:
        pairs = [
            (self.reference.scores[j], candidate.scores[j])
            for j in self.job_ids
            if j in self.reference.scores and j in candidate.scores
        ]
        if not pairs:
            return {"compared": 0, "mean_abs_diff": None, "max_abs_diff": None, "within_10": 0}
        diffs = [abs(a - b) for a, b in pairs]
        return {
            "compared": len(pairs),
            "missing": len(self.job_ids) - len(candidate.scores),
            "mean_abs_diff": round(sum(diffs) / len(diffs), 1),
            "max_abs_diff": max(diffs),
            "within_10": sum(d <= 10 for d in diffs),
            "rank_agreement": _rank_agreement(pairs),
        }


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
    configuration: Configuration, system: str, postings: Sequence[PostingInput]
) -> ConfigResult:
    result = ConfigResult(configuration.name)
    schema = output_json_schema()
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
        detail: dict[str, Any] = {
            "postings": len(chunk),
            "status": reply.status,
            "error": reply.error,
            "returned": len(items) if isinstance(items, list) else None,
            "valid": 0,
            "rejected": 0,
            "first_rejection": None,
            "unknown_ids": [],
            "missing_ids": [],
            "input_tokens": reply.input_tokens,
            "output_tokens": reply.output_tokens,
            "cost_usd": reply.cost_usd,
            "seconds": round(time.monotonic() - call_started, 1),
            "extra": reply.extra,
        }
        result.calls_detail.append(detail)
        if not isinstance(items, list):
            result.failed_calls += 1
            if reply.status == "usage_limit":
                break
            continue
        wanted = {p.job_id for p in chunk}
        for item in items:
            try:
                parsed = ScoreResult.model_validate(item)
            except ValidationError as exc:
                detail["rejected"] += 1
                detail["first_rejection"] = detail["first_rejection"] or str(exc)[:500]
                continue
            if parsed.job_id not in wanted:
                detail["unknown_ids"].append(parsed.job_id)
            elif parsed.job_id not in result.scores:
                detail["valid"] += 1
                result.scores[parsed.job_id] = parsed.overall_score
                result.summaries[parsed.job_id] = parsed.summary
        detail["missing_ids"] = sorted(wanted - result.scores.keys())
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
    return Calibration(
        job_ids=[g.canonical.id for g in groups],
        titles={g.canonical.id: g.canonical.title for g in groups},
        companies={g.canonical.id: g.canonical.company or "" for g in groups},
        reference=_score_with(reference, system, postings),
        candidates=[_score_with(c, system, postings) for c in candidates],
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
