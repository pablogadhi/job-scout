"""Calibration bookkeeping with a fake scorer (no spend): per-call detail in the report."""

import json
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from job_scout.config import AppConfig
from job_scout.scoring.backends.base import CallRequest, CallResult
from job_scout.scoring.calibrate import Configuration, run_calibration, save
from tests.scoring_helpers import FakeScorer, result_for, seed_jobs


def short_and_invalid(request: CallRequest) -> CallResult:
    """Scores the first job, sends an invalid item and an unknown id, omits the rest."""
    first = request.job_ids[0]
    items = [result_for(first), result_for(request.job_ids[1], score=500), result_for(9_999)]
    return CallResult("ok", payload={"results": items}, input_tokens=100, output_tokens=40)


def test_calls_detail(
    app_config: AppConfig, session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    ids = seed_jobs(session_factory, 5)
    reference = Configuration("ref x1", FakeScorer(), 1)
    candidate = Configuration("cand x5", FakeScorer(short_and_invalid), 5)
    broken = Configuration(
        "broken x5", FakeScorer(lambda r: CallResult("error", error="exit code 1")), 5
    )
    with session_factory() as session:
        result = run_calibration(app_config, session, ids, reference, [candidate, broken])

    assert len(result.reference.calls_detail) == 5
    assert all(c.valid == 1 for c in result.reference.calls_detail)
    (call,) = result.candidates[0].calls_detail
    assert (call.postings, call.returned, call.valid, call.rejected) == (5, 3, 1, 1)
    assert call.unknown_ids == [9_999] and len(call.missing_ids) == 4
    assert "overall_score" in (call.first_rejection or "")
    assert (call.input_tokens, call.output_tokens) == (100, 40)
    assert result.candidates[0].input_tokens == 100
    (failed,) = result.candidates[1].calls_detail
    assert (failed.status, failed.error, failed.returned) == ("error", "exit code 1", None)
    assert result.candidates[1].failed_calls == 1

    report = json.loads(save(result, tmp_path).read_text())
    assert report["candidates"][0]["calls_detail"][0]["valid"] == 1
