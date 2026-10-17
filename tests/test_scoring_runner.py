"""The scoring run: queueing, call limits, failure handling, backend switching, batches."""

import json
import re
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from job_scout.config import AppConfig
from job_scout.db.models import (
    Backend,
    CallStatus,
    Job,
    Run,
    Score,
    ScoreStatus,
    ScoringCall,
    ScoringMode,
    StopReason,
)
from job_scout.db.types import utcnow
from job_scout.scoring.backends.api import ApiScorer
from job_scout.scoring.backends.base import CallRequest, CallResult
from job_scout.scoring.planner import Limits
from job_scout.scoring.queue import pending_count, requeue_for_rescore
from job_scout.scoring.runner import RunOptions, ScoringRunner
from tests.scoring_helpers import FakeScorer, call_sizes, ok_all, result_for, seed_jobs

DAILY_CLI = Limits(
    postings_per_call=20, max_calls=5, max_input_tokens=60_000, max_output_tokens=12_000
)


def options(limits: Limits = DAILY_CLI, **kwargs: Any) -> RunOptions:
    return RunOptions(mode=ScoringMode.DAILY, limits=limits, **kwargs)


def scores(sf: sessionmaker[Session]) -> dict[int, Score]:
    with sf() as session:
        return {s.job_id: s for s in session.scalars(select(Score))}


def runner(
    app_config: AppConfig, sf: sessionmaker[Session], scorer: Any, **kw: Any
) -> ScoringRunner:
    return ScoringRunner(app_config, sf, scorer, **kw)


def test_scores_everything_pending(
    app_config: AppConfig, session_factory: sessionmaker[Session]
) -> None:
    ids = seed_jobs(session_factory, 25)
    seed_jobs(session_factory, 3, source="other", start=100, passed=False)  # dropped by pre-filter
    scorer = FakeScorer()
    report = runner(app_config, session_factory, scorer).run(options())

    assert report.stop_reason == StopReason.DONE
    assert (report.scored, report.calls_made, report.pending_left) == (25, 2, 0)
    assert call_sizes(scorer) == [20, 5]
    assert scorer.requests[0].job_ids[0] == ids[-1]  # newest first
    stored = scores(session_factory)
    assert set(stored) == set(ids)
    score = stored[ids[0]]
    assert score.status == ScoreStatus.SCORED and score.overall_score == 70
    assert score.backend == Backend.CLI and score.model == "claude-haiku-5-5"
    assert score.profile_hash == app_config.profile.sha256
    assert score.result is not None
    assert score.result["matched_skills"] == ["Python", "Django"]
    with session_factory() as session:
        run = session.scalars(select(Run)).one()
        assert (run.stop_reason, run.calls_made, run.postings_scored) == (StopReason.DONE, 2, 25)
        assert len(session.scalars(select(ScoringCall)).all()) == 2

    # Nothing is scored twice.
    again = runner(app_config, session_factory, FakeScorer()).run(options())
    assert (again.calls_made, again.stop_reason) == (0, StopReason.DONE)


def test_max_calls_leaves_rest_pending(
    app_config: AppConfig, session_factory: sessionmaker[Session]
) -> None:
    seed_jobs(session_factory, 30)
    limits = Limits(
        postings_per_call=10, max_calls=2, max_input_tokens=None, max_output_tokens=None
    )
    report = runner(app_config, session_factory, FakeScorer()).run(options(limits))
    assert (report.stop_reason, report.scored, report.pending_left) == (
        StopReason.MAX_CALLS,
        20,
        10,
    )


def test_usage_limit_stops_without_retrying(
    app_config: AppConfig, session_factory: sessionmaker[Session]
) -> None:
    seed_jobs(session_factory, 30)
    calls = 0

    def behave(request: CallRequest) -> CallResult:
        nonlocal calls
        calls += 1
        return (
            ok_all(request)
            if calls == 1
            else CallResult("usage_limit", error="usage limit reached")
        )

    report = runner(app_config, session_factory, FakeScorer(behave)).run(options())
    assert report.stop_reason == StopReason.USAGE_LIMIT
    assert (report.calls_made, report.scored, report.pending_left) == (2, 20, 10)
    assert all(s.attempts == 0 for s in scores(session_factory).values())  # not their fault


def test_failed_call_is_split_and_retried_once(
    app_config: AppConfig, session_factory: sessionmaker[Session]
) -> None:
    ids = seed_jobs(session_factory, 4)
    poison = ids[0]

    def behave(request: CallRequest) -> CallResult:
        if poison in request.job_ids:
            return CallResult("invalid", error="schema validation failed")
        return ok_all(request)

    scorer = FakeScorer(behave)
    report = runner(app_config, session_factory, scorer).run(options())
    # 4 fail together, then halves of 2: one half succeeds, the poisoned half fails again.
    assert call_sizes(scorer) == [4, 2, 2]
    halves = [set(r.job_ids) for r in scorer.requests[1:]]
    assert halves[0] | halves[1] == set(ids) and not halves[0] & halves[1]
    poisoned_half = next(h for h in halves if poison in h)
    stored = scores(session_factory)
    for job_id in ids:
        expected = ScoreStatus.SCORE_FAILED if job_id in poisoned_half else ScoreStatus.SCORED
        assert stored[job_id].status == expected, job_id
    assert stored[poison].attempts == 2
    assert report.failed == 2
    assert report.stop_reason == StopReason.DONE  # nothing left pending


@pytest.mark.parametrize("status", ["refusal", "truncated", "timeout", "error"])
def test_single_posting_retried_once_then_failed(
    app_config: AppConfig, session_factory: sessionmaker[Session], status: str
) -> None:
    (job,) = seed_jobs(session_factory, 1)
    scorer = FakeScorer(lambda r: CallResult(status, error="nope"))  # type: ignore[arg-type]
    report = runner(app_config, session_factory, scorer).run(options())
    assert call_sizes(scorer) == [1, 1]
    assert scores(session_factory)[job].status == ScoreStatus.SCORE_FAILED
    assert report.failed == 1


def test_missing_and_unknown_ids(
    app_config: AppConfig, session_factory: sessionmaker[Session]
) -> None:
    a, b, c = seed_jobs(session_factory, 3)

    def behave(request: CallRequest) -> CallResult:
        return CallResult(
            "ok",
            payload={
                "results": [result_for(c), result_for(a), result_for(9999), result_for(c, 10)]
            },
        )

    report = runner(app_config, session_factory, FakeScorer(behave)).run(options())
    stored = scores(session_factory)
    assert stored[a].status == stored[c].status == ScoreStatus.SCORED
    assert stored[c].overall_score == 70  # duplicate result ignored, first kept
    assert (
        stored[b].status == ScoreStatus.PENDING and stored[b].attempts == 1
    )  # missing: back to pending
    assert 9999 not in stored  # unknown id discarded
    assert report.pending_left == 1 and report.stop_reason == StopReason.MAX_CALLS


def test_invalid_item_fails_only_that_job(
    app_config: AppConfig, session_factory: sessionmaker[Session]
) -> None:
    a, b = seed_jobs(session_factory, 2)

    def behave(request: CallRequest) -> CallResult:
        return CallResult(
            "ok", payload={"results": [result_for(a), result_for(b, overall_score=150)]}
        )

    runner(app_config, session_factory, FakeScorer(behave)).run(options())
    stored = scores(session_factory)
    assert stored[a].status == ScoreStatus.SCORED
    assert stored[b].status == ScoreStatus.PENDING and stored[b].attempts == 1


def test_switching_backend_mid_backlog(
    app_config: AppConfig, session_factory: sessionmaker[Session]
) -> None:
    seed_jobs(session_factory, 6)
    limits = Limits(postings_per_call=3, max_calls=1, max_input_tokens=None, max_output_tokens=None)
    first = runner(app_config, session_factory, FakeScorer()).run(options(limits))
    assert (first.scored, first.pending_left) == (3, 3)
    api_limits = Limits(
        postings_per_call=1, max_calls=500, max_input_tokens=None, max_output_tokens=None
    )
    second = runner(app_config, session_factory, FakeScorer(backend=Backend.API)).run(
        options(api_limits)
    )
    assert (second.scored, second.stop_reason) == (3, StopReason.DONE)
    backends = sorted((s.backend for s in scores(session_factory).values()), key=str)
    assert backends == [Backend.API] * 3 + [Backend.CLI] * 3


def test_spend_guard(app_config: AppConfig, session_factory: sessionmaker[Session]) -> None:
    seed_jobs(session_factory, 10)
    expensive = FakeScorer(lambda r: ok_all(r, cost=2.0), backend=Backend.API)  # max_spend 5.00
    limits = Limits(
        postings_per_call=1, max_calls=500, max_input_tokens=None, max_output_tokens=None
    )
    report = runner(app_config, session_factory, expensive).run(options(limits))
    assert (report.stop_reason, report.calls_made, report.scored) == (StopReason.SPEND_LIMIT, 3, 3)
    assert report.cost_usd == pytest.approx(6.0)


def test_cli_cost_is_not_a_spend_limit(
    app_config: AppConfig, session_factory: sessionmaker[Session]
) -> None:
    seed_jobs(session_factory, 3)
    limits = Limits(postings_per_call=1, max_calls=5, max_input_tokens=None, max_output_tokens=None)
    report = runner(app_config, session_factory, FakeScorer(lambda r: ok_all(r, cost=9.0))).run(
        options(limits)
    )
    assert report.stop_reason == StopReason.DONE  # Pro: cost is an API-equivalent estimate


def test_dry_run_plans_without_calls(
    app_config: AppConfig, session_factory: sessionmaker[Session]
) -> None:
    seed_jobs(session_factory, 25)
    scorer = FakeScorer()
    report = runner(app_config, session_factory, scorer).run(options(dry_run=True))
    assert report.planned_calls == [20, 5] and scorer.requests == []
    assert report.enqueued == (25, 0) and report.pending_left == 25
    with session_factory() as session:
        assert session.scalar(select(Run.id)) is None
        assert session.scalar(select(Score.id)) is None  # nothing queued for real


def test_group_gaining_a_copy_is_requeued(
    app_config: AppConfig, session_factory: sessionmaker[Session]
) -> None:
    (job_id,) = seed_jobs(session_factory, 1)
    runner(app_config, session_factory, FakeScorer()).run(options())
    with session_factory.begin() as session:
        original = session.get_one(Job, job_id)
        session.add(
            Job(
                source_id="board",
                source_job_id="copy",
                canonical_url="https://board.example/copy",
                title=original.title,
                title_normalized=original.title_normalized,
                location_raw="Mexico (remote)",
                first_seen_at=utcnow() + timedelta(minutes=1),
                last_seen_at=utcnow(),
                raw={},
                dedupe_key=original.dedupe_key,
                duplicate_of=job_id,
            )
        )
    scorer = FakeScorer()
    report = runner(app_config, session_factory, scorer).run(options())
    assert report.enqueued == (0, 1) and report.scored == 1
    assert "Mexico (remote)" in json.dumps([r.user for r in scorer.requests])


def test_rescore_queues_top_and_recent(
    app_config: AppConfig, session_factory: sessionmaker[Session]
) -> None:
    ids = seed_jobs(session_factory, 5)
    runner(app_config, session_factory, FakeScorer()).run(options())
    with session_factory.begin() as session:
        for rank, job_id in enumerate(ids):
            session.scalars(select(Score).where(Score.job_id == job_id)).one().overall_score = (
                rank * 10
            )
        assert requeue_for_rescore(session, app_config.profile.sha256, top=2) == 2
        assert pending_count(session) == 2
        top_two = {
            s.job_id
            for s in session.scalars(select(Score).where(Score.status == ScoreStatus.PENDING))
        }
    assert top_two == set(ids[-2:])


def test_editing_the_profile_rescores_nothing(
    app_config: AppConfig, session_factory: sessionmaker[Session]
) -> None:
    ids = seed_jobs(session_factory, 2)
    runner(app_config, session_factory, FakeScorer()).run(options())
    (leftover,) = seed_jobs(session_factory, 1, start=3)
    with session_factory.begin() as session:  # pending since before an older profile edit
        session.add(Score(job_id=leftover, profile_hash="old"))
    edited = replace(app_config, profile=replace(app_config.profile, sha256="edited"))
    scorer = FakeScorer()
    report = runner(edited, session_factory, scorer).run(options())
    assert report.enqueued == (0, 0) and report.scored == 1  # only the leftover pending job
    by_job = scores(session_factory)
    assert {by_job[i].profile_hash for i in ids} == {app_config.profile.sha256}  # kept
    assert by_job[leftover].profile_hash == "edited"  # scored now, under the profile that scored it


# --- Message Batches ---------------------------------------------------------------------


class BatchClient:
    """Fake client: batches end after `ready_after` polls; every request succeeds."""

    def __init__(self, ready_after: int = 2) -> None:
        self.ready_after = ready_after
        self.polls = 0
        self.submitted: list[list[dict[str, Any]]] = []
        self.messages = SimpleNamespace(batches=self)

    def create(self, requests: list[dict[str, Any]]) -> Any:
        self.submitted.append(requests)
        return SimpleNamespace(id=f"msgbatch_{len(self.submitted)}")

    def retrieve(self, batch_id: str) -> Any:
        self.polls += 1
        return SimpleNamespace(
            processing_status="ended" if self.polls >= self.ready_after else "in_progress"
        )

    def results(self, batch_id: str) -> Any:
        requests = self.submitted[int(batch_id.split("_")[1]) - 1]
        for request in requests:
            user = request["params"]["messages"][0]["content"]
            job_ids = [int(i) for i in re.findall(r'<posting job_id="(\d+)">', user)]
            text = json.dumps({"results": [result_for(i) for i in job_ids]})
            message = SimpleNamespace(
                id="m",
                stop_reason="end_turn",
                stop_details=None,
                content=[SimpleNamespace(type="text", text=text)],
                usage=SimpleNamespace(
                    input_tokens=3000,
                    output_tokens=450,
                    cache_creation_input_tokens=0,
                    cache_read_input_tokens=0,
                ),
            )
            yield SimpleNamespace(
                custom_id=request["custom_id"],
                result=SimpleNamespace(type="succeeded", message=message),
            )


def batch_runner(
    app_config: AppConfig, sf: sessionmaker[Session], client: BatchClient, clock: list[float]
) -> ScoringRunner:
    scorer = ApiScorer(app_config.settings.scoring.api, "claude-haiku-4-5", client=client)  # type: ignore[arg-type]

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    return ScoringRunner(app_config, sf, scorer, sleep=sleep, clock=lambda: clock[0])


BACKLOG_API = Limits(
    postings_per_call=1, max_calls=10_000, max_input_tokens=None, max_output_tokens=None
)


def test_batches_api_end_to_end(
    app_config: AppConfig, session_factory: sessionmaker[Session]
) -> None:
    seed_jobs(session_factory, 4)
    client = BatchClient(ready_after=3)
    report = batch_runner(app_config, session_factory, client, [0.0]).run(
        RunOptions(mode=ScoringMode.BACKLOG, limits=BACKLOG_API, use_batches_api=True)
    )
    assert report.stop_reason == StopReason.DONE
    assert (report.scored, report.calls_made, report.batch_id) == (4, 4, "msgbatch_1")
    assert len(client.submitted) == 1 and len(client.submitted[0]) == 4
    assert report.cost_usd == pytest.approx(4 * (3000 + 450 * 5) / 1e6 * 0.5)
    with session_factory() as session:
        call = session.scalars(select(ScoringCall)).one()
        assert (call.status, call.api_batch_id, call.postings_count) == (
            CallStatus.SUCCEEDED,
            "msgbatch_1",
            4,
        )
        assert call.request_map is not None
    assert set(call.request_map) == {"c0", "c1", "c2", "c3"}


def test_batch_resumes_after_restart(
    app_config: AppConfig, session_factory: sessionmaker[Session]
) -> None:
    seed_jobs(session_factory, 3)
    client = BatchClient(ready_after=10_000)  # never ends during the first run
    clock = [0.0]
    first = batch_runner(app_config, session_factory, client, clock).run(
        RunOptions(mode=ScoringMode.BACKLOG, limits=BACKLOG_API, use_batches_api=True)
    )
    assert first.stop_reason == StopReason.BATCH_PENDING and first.scored == 0
    # In-flight jobs are not handed to another run or backend meanwhile.
    cli = FakeScorer()
    runner(app_config, session_factory, cli).run(options())
    assert cli.requests == []

    client.ready_after = 0  # the batch finished while we were away
    second = batch_runner(app_config, session_factory, client, clock).run(
        RunOptions(mode=ScoringMode.BACKLOG, limits=BACKLOG_API, use_batches_api=True)
    )
    assert second.stop_reason == StopReason.DONE and second.scored == 3
    assert len(client.submitted) == 1  # resumed from the stored batch id, not resubmitted


def test_batch_spend_guard_trims_the_batch(
    app_config: AppConfig, session_factory: sessionmaker[Session]
) -> None:
    seed_jobs(session_factory, 5)
    api = app_config.settings.scoring.api.model_copy(update={"max_spend_usd_per_run": 0.01})
    settings = app_config.settings.model_copy(
        update={"scoring": app_config.settings.scoring.model_copy(update={"api": api})}
    )
    config = AppConfig(
        app_config.config_dir, settings, app_config.sources, app_config.filters, app_config.profile
    )
    client = BatchClient(ready_after=1)
    report = batch_runner(config, session_factory, client, [0.0]).run(
        RunOptions(mode=ScoringMode.BACKLOG, limits=BACKLOG_API, use_batches_api=True)
    )
    assert report.stop_reason == StopReason.SPEND_LIMIT
    assert 0 < len(client.submitted[0]) < 5
    assert report.pending_left == 5 - len(client.submitted[0])
