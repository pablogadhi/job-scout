"""Pick your recent judgments to show the scorer as examples.

Disagreements teach the most (a 👎 on a job Claude scored above the digest threshold, a 👍 on
one it scored below), so they come first; then up and down votes are balanced, newest first.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from job_scout.db.models import Feedback, Job, Vote

_CANDIDATES = 200  # only the most recent judgments are considered


@dataclass(frozen=True)
class Example:
    job_id: int
    title: str
    company: str | None
    vote: Vote
    reason: str | None
    overall_score: int | None
    disqualified: bool | None
    summary: str | None
    profile_hash: str | None  # the profile the score was made under
    updated_at: datetime

    def claude_positive(self, min_score: int) -> bool | None:
        """Did Claude's score put the job in the digest? None if it was never scored."""
        if self.overall_score is None:
            return None
        return not self.disqualified and self.overall_score >= min_score

    def disagrees(self, min_score: int) -> bool:
        positive = self.claude_positive(min_score)
        return positive is not None and positive != (self.vote is Vote.UP)


def load_judgments(session: Session, limit: int = _CANDIDATES) -> list[Example]:
    """Recent judgments, newest first."""
    rows = session.execute(
        select(Feedback, Job.title, Job.company)
        .join(Job, Job.id == Feedback.job_id)
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
        )
        for f, title, company in rows
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
