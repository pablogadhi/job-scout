"""Pick your recent judgments to show the scorer as examples.

Disagreements teach the most (a 👎 on a job scored above the digest threshold, a 👍 on one
scored below), so they come first; then up and down votes are balanced, newest first. A
job's score is its current one: scoring.yaml can change it after the vote.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from job_scout.db.models import Feedback, Job, Score, ScoreStatus, Vote
from job_scout.scoring.points import Breakdown

_CANDIDATES = 200  # only the most recent judgments are considered


@dataclass(frozen=True)
class Example:
    job_id: int
    title: str
    company: str | None
    vote: Vote
    reason: str | None
    overall_score: int | None  # the score you voted on
    disqualified: bool | None
    summary: str | None
    profile_hash: str | None  # the profile the score was made under
    updated_at: datetime
    current_score: int | None = None  # now, under the current scoring.yaml
    current_disqualified: bool | None = None
    breakdown_line: str | None = None  # how the current score was computed

    @property
    def score(self) -> int | None:
        return self.current_score if self.current_score is not None else self.overall_score

    @property
    def score_disqualified(self) -> bool | None:
        return self.current_disqualified if self.current_score is not None else self.disqualified

    def claude_positive(self, min_score: int) -> bool | None:
        """Does the job's score put it in the digest? None if it was never scored."""
        if self.score is None:
            return None
        return not self.score_disqualified and self.score >= min_score

    def disagrees(self, min_score: int) -> bool:
        positive = self.claude_positive(min_score)
        return positive is not None and positive != (self.vote is Vote.UP)


def load_judgments(session: Session, limit: int = _CANDIDATES) -> list[Example]:
    """Recent judgments, newest first."""
    rows = session.execute(
        select(Feedback, Job.title, Job.company, Score)
        .join(Job, Job.id == Feedback.job_id)
        .outerjoin(Score, (Score.job_id == Feedback.job_id) & (Score.status == ScoreStatus.SCORED))
        .order_by(Feedback.updated_at.desc(), Feedback.id.desc())
        .limit(limit)
    )
    return [
        Example(
            job_id=f.job_id,
            title=title,
            company=company,
            vote=f.vote,
            reason=f.reason,
            overall_score=f.overall_score,
            disqualified=f.disqualified,
            summary=f.summary,
            profile_hash=f.profile_hash,
            updated_at=f.updated_at,
            current_score=score.overall_score if score else None,
            current_disqualified=score.disqualified if score else None,
            breakdown_line=Breakdown.from_json(score.breakdown).line()
            if score and score.breakdown
            else None,
        )
        for f, title, company, score in rows
    ]


def pick_examples(judgments: list[Example], limit: int, min_score: int) -> list[Example]:
    """Up to `limit` judgments, disagreements first, about half of each vote; oldest first."""
    if limit <= 0:
        return []
    newest_first = sorted(judgments, key=lambda e: e.updated_at, reverse=True)
    ranked = sorted(newest_first, key=lambda e: not e.disagrees(min_score))  # stable
    cap = -(-limit // 2)
    chosen: list[Example] = []
    for example in ranked:
        if len(chosen) < limit and sum(c.vote is example.vote for c in chosen) < cap:
            chosen.append(example)
    for example in ranked:  # one vote ran short: fill up with the other
        if len(chosen) < limit and example not in chosen:
            chosen.append(example)
    return sorted(chosen, key=lambda e: e.updated_at)


def select_examples(session: Session, limit: int, min_score: int) -> list[Example]:
    if limit <= 0:
        return []
    return pick_examples(load_judgments(session), limit, min_score)
