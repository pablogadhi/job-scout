"""Scoring backends: `cli` (claude -p, Pro subscription) and `api` (Anthropic SDK)."""

from __future__ import annotations

from collections.abc import Mapping

from job_scout.config.settings import Scoring
from job_scout.db.models import Backend
from job_scout.scoring.backends.api import ApiScorer
from job_scout.scoring.backends.base import CallRequest, CallResult, Scorer
from job_scout.scoring.backends.cli import CliScorer


def make_scorer(scoring: Scoring, backend: Backend, environ: Mapping[str, str]) -> Scorer:
    if backend == Backend.CLI:
        return CliScorer(scoring.cli, scoring.model, environ=environ, effort=scoring.effort)
    if backend == Backend.API:
        return ApiScorer(scoring.api, scoring.model, environ=environ, effort=scoring.effort)
    raise ValueError(f"unknown backend {backend!r}")


class PlanOnlyScorer:
    """For dry runs: has a backend and model, refuses to score."""

    def __init__(self, backend: Backend, model: str) -> None:
        self.backend = backend
        self.model = model

    def score(self, request: CallRequest) -> CallResult:
        raise RuntimeError("dry run: no calls are made")
