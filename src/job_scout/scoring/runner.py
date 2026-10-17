"""Score pending job groups with one backend (PLAN.md §7).

Every call's outcome is committed before the next call, so an interrupted run loses
nothing; whatever wasn't scored stays pending for the next run.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from job_scout.config import AppConfig
from job_scout.config.settings import ModelPrices
from job_scout.db.groups import load_groups
from job_scout.db.models import (
    Backend,
    CallStatus,
    Run,
    Score,
    ScoreStatus,
    ScoringCall,
    ScoringMode,
    StopReason,
)
from job_scout.db.types import utcnow
from job_scout.feedback.examples import select_examples
from job_scout.scoring.backends.api import ApiScorer
from job_scout.scoring.backends.base import CallRequest, CallResult, Scorer
from job_scout.scoring.planner import BatchPlanner, Limits
from job_scout.scoring.prompt import (
    CHARS_PER_TOKEN,
    COST_OUTPUT_TOKENS_PER_POSTING,
    OUTPUT_TOKENS_PER_POSTING,
    PostingInput,
    render_posting,
    system_prompt,
    user_message,
)
from job_scout.scoring.queue import MAX_ATTEMPTS, enqueue, pending_count, pending_query
from job_scout.scoring.schema import ScoreResult, check_profile_matches, output_json_schema

log = logging.getLogger(__name__)

API_MAX_TOKENS = 16_000  # keeps non-streaming requests well inside SDK timeouts

_CALL_STATUS = {
    "ok": CallStatus.SUCCEEDED,
    "usage_limit": CallStatus.USAGE_LIMIT,
}


@dataclass
class RunOptions:
    mode: ScoringMode
    limits: Limits
    command: str = "score"
    dry_run: bool = False
    use_batches_api: bool = False


@dataclass
class RunReport:
    run_id: int | None = None
    stop_reason: StopReason | None = None
    calls_made: int = 0
    scored: int = 0
    failed: int = 0
    pending_left: int = 0
    cost_usd: float = 0.0
    planned_calls: list[int] = field(default_factory=list)  # postings per planned call
    estimated_cost_usd: float | None = None  # dry run, api backend: planned calls at list price
    enqueued: tuple[int, int] = (0, 0)
    batch_id: str | None = None


@dataclass
class _Outcome:
    scored: list[int] = field(default_factory=list)
    retry: list[int] = field(default_factory=list)  # failed, still under MAX_ATTEMPTS
    failed: list[int] = field(default_factory=list)  # reached MAX_ATTEMPTS: score_failed


def max_output_tokens(postings: int) -> int:
    return min(API_MAX_TOKENS, 1_000 + 2 * OUTPUT_TOKENS_PER_POSTING * postings)


class ScoringRunner:
    def __init__(
        self,
        config: AppConfig,
        session_factory: sessionmaker[Session],
        scorer: Scorer,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        check_profile_matches(config.profile)
        self.config = config
        self.sf = session_factory
        self.scorer = scorer
        self.sleep = sleep
        self.clock = clock
        self.profile_hash = config.profile.sha256
        # Your recent judgments go in as examples; the same set for every call of this run.
        with session_factory() as session:
            examples = select_examples(
                session,
                config.settings.feedback.prompt_examples,
                config.settings.digest.min_score,
            )
        self.feedback_job_ids = [e.job_id for e in examples] or None
        self.system = system_prompt(config.profile, examples)
        self.schema = output_json_schema()

    # --- entry point ---------------------------------------------------------------------

    def run(self, options: RunOptions) -> RunReport:
        report = RunReport()
        planner = BatchPlanner(options.limits, len(self.system) // CHARS_PER_TOKEN + 1)
        if options.dry_run:
            # Plan against what a real run would queue, then roll it all back: no writes.
            with self.sf() as session:
                report.enqueued = enqueue(session, self.profile_hash)
                plan = planner.plan(self._pending_postings(session))
                report.pending_left = pending_count(session)
                session.rollback()
            report.planned_calls = [len(call) for call in plan]
            prices = self.config.settings.scoring.api.prices.get(self.scorer.model)
            if self.scorer.backend == Backend.API and prices is not None:
                discount = self.config.settings.scoring.api.batch_discount
                factor = discount if options.use_batches_api else 1.0
                report.estimated_cost_usd = sum(self._estimate(c, prices, factor) for c in plan)
            return report

        with self.sf.begin() as session:
            report.enqueued = enqueue(session, self.profile_hash)
        with self.sf() as session:
            plan = planner.plan(self._pending_postings(session))
        report.planned_calls = [len(call) for call in plan]

        with self.sf.begin() as session:
            run = Run(
                command=options.command,
                mode=options.mode,
                backend=self.scorer.backend,
                started_at=utcnow(),
            )
            session.add(run)
            session.flush()
            report.run_id = run.id

        try:
            if options.use_batches_api and isinstance(self.scorer, ApiScorer):
                self._run_batches(report, options, planner)
            else:
                self._run_calls(report, options, deque(plan))
        except Exception:
            report.stop_reason = StopReason.ERROR
            log.exception("scoring run %s failed", report.run_id)
            raise
        finally:
            self._finish(report)
        return report

    # --- one call at a time (cli, api without batches) ------------------------------------

    def _run_calls(
        self, report: RunReport, options: RunOptions, queue: deque[list[PostingInput]]
    ) -> None:
        max_spend = self.config.settings.scoring.api.max_spend_usd_per_run
        is_api = self.scorer.backend == Backend.API
        while queue:
            if report.calls_made >= options.limits.max_calls:
                report.stop_reason = StopReason.MAX_CALLS
                return
            if is_api and report.cost_usd >= max_spend:
                report.stop_reason = StopReason.SPEND_LIMIT
                return
            call = queue.popleft()
            result = self.scorer.score(self._request(call))
            report.calls_made += 1
            report.cost_usd += result.cost_usd or 0.0
            outcome = self._record(report, options.mode, call, result)
            if result.status == "usage_limit":
                log.warning("usage limit reached; stopping this run: %s", result.error)
                report.stop_reason = StopReason.USAGE_LIMIT
                return
            if outcome.retry and result.status != "ok":
                # Whole call failed: retry once, split in half so one bad posting can't sink both.
                retry = [p for p in call if p.job_id in set(outcome.retry)]
                half = (len(retry) + 1) // 2
                for part in reversed([retry[:half], retry[half:]]):
                    if part:
                        queue.appendleft(part)

    def _request(self, call: Sequence[PostingInput]) -> CallRequest:
        return CallRequest(
            system=self.system,
            user=user_message(list(call)),
            job_ids=tuple(p.job_id for p in call),
            schema=self.schema,
            max_output_tokens=max_output_tokens(len(call)),
        )

    def _record(
        self,
        report: RunReport,
        mode: ScoringMode,
        call: Sequence[PostingInput],
        result: CallResult,
        scoring_call: ScoringCall | None = None,
    ) -> _Outcome:
        """Store a call and its scores in one transaction; returns what happened to each job."""
        with self.sf.begin() as session:
            if scoring_call is None:
                scoring_call = ScoringCall(
                    run_id=report.run_id,
                    backend=self.scorer.backend,
                    model=self.scorer.model,
                    mode=mode,
                    postings_count=len(call),
                    status=_CALL_STATUS.get(result.status, CallStatus.FAILED),
                    exit_code=result.exit_code,
                    http_status=result.http_status,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    cost_usd_estimate=result.cost_usd,
                    duration_ms=result.duration_ms,
                    error=result.error if result.status != "ok" else None,
                    feedback_job_ids=self.feedback_job_ids,
                )
                session.add(scoring_call)
                session.flush()
            else:
                scoring_call = session.merge(scoring_call)
            outcome = self._apply(session, scoring_call, call, result)
        report.scored += len(outcome.scored)
        report.failed += len(outcome.failed)
        if result.status not in ("ok", "usage_limit"):
            log.warning("call failed (%s): %s", result.status, result.error)
        return outcome

    def _apply(
        self,
        session: Session,
        scoring_call: ScoringCall,
        call: Sequence[PostingInput],
        result: CallResult,
    ) -> _Outcome:
        ids = [p.job_id for p in call]
        scores = {s.job_id: s for s in session.scalars(select(Score).where(Score.job_id.in_(ids)))}
        outcome = _Outcome()
        if result.status == "usage_limit":
            return outcome  # not the postings' fault: they stay pending, no attempt counted
        if result.status == "error" and result.extra.get("retry_free"):
            return outcome  # canceled/expired batch request: retry later at no attempt cost

        valid: dict[int, ScoreResult] = {}
        bad: set[int] = set()
        if result.status == "ok":
            items = result.payload.get("results") if isinstance(result.payload, dict) else None
            if not isinstance(items, list):
                bad = set(ids)
                scoring_call.status = CallStatus.FAILED
                scoring_call.error = "reply has no results list"
            else:
                for item in items:
                    job_id = item.get("job_id") if isinstance(item, dict) else None
                    if job_id not in scores:
                        log.warning("discarding result for unknown job id %r", job_id)
                        continue
                    if job_id in valid:
                        log.warning("duplicate result for job %s; keeping the first", job_id)
                        continue
                    try:
                        valid[job_id] = ScoreResult.model_validate(item)
                    except ValidationError as exc:
                        log.warning("invalid result for job %s: %s", job_id, exc.errors()[:3])
                        bad.add(job_id)
                missing = set(ids) - set(valid) - bad
                if missing:
                    log.warning("no result for jobs %s; they go back to pending", sorted(missing))
                bad |= missing
        else:
            bad = set(ids)

        now = utcnow()
        share = len(valid) or 1
        for job_id, parsed in valid.items():
            score = scores[job_id]
            score.status = ScoreStatus.SCORED
            score.profile_hash = self.profile_hash  # the profile this result was made under
            score.result = parsed.model_dump()
            score.overall_score = parsed.overall_score
            score.disqualified = parsed.disqualified
            score.backend = self.scorer.backend
            score.model = self.scorer.model
            score.call_id = scoring_call.id
            score.scored_at = now
            score.input_tokens = result.input_tokens // share if result.input_tokens else None
            score.output_tokens = result.output_tokens // share if result.output_tokens else None
            score.cost_usd_estimate = result.cost_usd / share if result.cost_usd else None
            outcome.scored.append(job_id)
        for job_id in sorted(bad):
            score = scores[job_id]
            score.attempts += 1
            score.call_id = scoring_call.id
            if score.attempts >= MAX_ATTEMPTS:
                score.status = ScoreStatus.SCORE_FAILED
                outcome.failed.append(job_id)
            else:
                outcome.retry.append(job_id)
        return outcome

    # --- Message Batches -------------------------------------------------------------------

    def _run_batches(self, report: RunReport, options: RunOptions, planner: BatchPlanner) -> None:
        assert isinstance(self.scorer, ApiScorer)
        scorer = self.scorer
        if not self._resume_batches(report, options):
            report.stop_reason = StopReason.BATCH_PENDING
            return

        api = self.config.settings.scoring.api
        prices = scorer.prices
        with self.sf() as session:
            plan = planner.plan(self._pending_postings(session))
        if not plan:
            return
        # Spend guard, up front: a batch is all-or-nothing once submitted.
        budget = api.max_spend_usd_per_run - report.cost_usd
        requests: dict[str, list[PostingInput]] = {}
        estimate = 0.0
        for index, call in enumerate(plan):
            cost = self._estimate(call, prices, api.batch_discount)
            if estimate + cost > budget:
                report.stop_reason = StopReason.SPEND_LIMIT
                break
            estimate += cost
            requests[f"c{index}"] = call
        if not requests:
            return

        with self.sf.begin() as session:
            scoring_call = ScoringCall(
                run_id=report.run_id,
                backend=scorer.backend,
                model=scorer.model,
                mode=options.mode,
                postings_count=sum(len(c) for c in requests.values()),
                status=CallStatus.RUNNING,
                request_map={cid: [p.job_id for p in call] for cid, call in requests.items()},
                feedback_job_ids=self.feedback_job_ids,
            )
            session.add(scoring_call)
            session.flush()
            # Mark the jobs as in flight before submitting, so no other run picks them up.
            for call in requests.values():
                for score in session.scalars(
                    select(Score).where(Score.job_id.in_([p.job_id for p in call]))
                ):
                    score.call_id = scoring_call.id
            call_id = scoring_call.id

        batch_id = scorer.submit_batch({cid: self._request(call) for cid, call in requests.items()})
        report.batch_id = batch_id
        report.calls_made += len(requests)
        with self.sf.begin() as session:
            stored = session.get_one(ScoringCall, call_id)
            stored.api_batch_id = batch_id
            stored.status = CallStatus.SUBMITTED
        log.info("submitted batch %s with %d requests", batch_id, len(requests))
        if not self._wait_and_ingest(report, options, call_id):
            report.stop_reason = StopReason.BATCH_PENDING

    def _resume_batches(self, report: RunReport, options: RunOptions) -> bool:
        """Finish batches a previous run submitted. False if one is still processing."""
        with self.sf() as session:
            ids = list(
                session.scalars(
                    select(ScoringCall.id).where(
                        ScoringCall.status == CallStatus.SUBMITTED,
                        ScoringCall.api_batch_id.is_not(None),
                    )
                )
            )
        return all(self._wait_and_ingest(report, options, call_id) for call_id in ids)

    def _wait_and_ingest(self, report: RunReport, options: RunOptions, call_id: int) -> bool:
        assert isinstance(self.scorer, ApiScorer)
        api = self.config.settings.scoring.api
        with self.sf() as session:
            scoring_call = session.get_one(ScoringCall, call_id)
            batch_id = scoring_call.api_batch_id
            request_map = dict(scoring_call.request_map or {})
        assert batch_id is not None
        deadline = self.clock() + api.batch_max_wait_minutes * 60
        while not self.scorer.batch_ended(batch_id):
            if self.clock() >= deadline:
                log.info("batch %s still processing; the next run will resume it", batch_id)
                return False
            self.sleep(api.batch_poll_seconds)

        input_tokens = output_tokens = 0
        cost = 0.0
        postings = {
            job_id: PostingInput(job_id, "") for ids in request_map.values() for job_id in ids
        }
        for custom_id, result in self.scorer.batch_results(batch_id):
            call = [postings[i] for i in request_map.get(custom_id, [])]
            if not call:
                log.warning("batch %s: unknown custom_id %r", batch_id, custom_id)
                continue
            with self.sf() as session:
                stored = session.get_one(ScoringCall, call_id)
            self._record(report, options.mode, call, result, scoring_call=stored)
            input_tokens += result.input_tokens or 0
            output_tokens += result.output_tokens or 0
            cost += result.cost_usd or 0.0
        report.cost_usd += cost
        with self.sf.begin() as session:
            stored = session.get_one(ScoringCall, call_id)
            stored.status = CallStatus.SUCCEEDED
            stored.input_tokens, stored.output_tokens = input_tokens, output_tokens
            stored.cost_usd_estimate = cost
            # Requests with no result at all: release their jobs back to the queue.
            for score in session.scalars(select(Score).where(Score.call_id == call_id)):
                if score.status == ScoreStatus.PENDING:
                    score.call_id = None
        log.info("ingested batch %s ($%.4f)", batch_id, cost)
        return True

    def _estimate(
        self, call: Sequence[PostingInput], prices: ModelPrices | None, discount: float
    ) -> float:
        if prices is None:
            return 0.0
        input_tokens = len(self.system) // CHARS_PER_TOKEN + sum(p.estimated_tokens for p in call)
        output_tokens = COST_OUTPUT_TOKENS_PER_POSTING * len(call)
        return (input_tokens * prices.input + output_tokens * prices.output) / 1_000_000 * discount

    # --- helpers ---------------------------------------------------------------------------

    def _pending_postings(self, session: Session) -> list[PostingInput]:
        max_chars = self.config.settings.scoring.description_max_chars
        order = self.config.settings.scoring.order
        ids = list(session.scalars(pending_query(order)))
        return [render_posting(g, max_chars) for g in load_groups(session, ids)]

    def _finish(self, report: RunReport) -> None:
        with self.sf.begin() as session:
            report.pending_left = pending_count(session)
            if report.stop_reason is None:
                report.stop_reason = (
                    StopReason.DONE if report.pending_left == 0 else StopReason.MAX_CALLS
                )
            if report.run_id is None:
                return
            run = session.get_one(Run, report.run_id)
            run.finished_at = utcnow()
            run.calls_made = report.calls_made
            run.postings_scored = report.scored
            run.postings_left_pending = report.pending_left
            run.stop_reason = report.stop_reason
