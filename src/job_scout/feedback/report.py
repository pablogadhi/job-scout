"""How Claude's scores compare with your votes (`job-scout feedback report`).

"Claude said yes" means the score you reacted to put the job in the digest: not
disqualified and at least digest.min_score. A 👍 below that or a 👎 above it is a
disagreement; recurring reasons there are candidates for profile.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from statistics import mean

from sqlalchemy.orm import Session

from job_scout.db.models import Vote
from job_scout.feedback.examples import Example, load_judgments


@dataclass
class FeedbackStats:
    min_score: int
    judgments: list[Example] = field(default_factory=list)

    def _with(self, vote: Vote, positive: bool | None) -> list[Example]:
        return [
            e
            for e in self.judgments
            if e.vote is vote and e.claude_positive(self.min_score) is positive
        ]

    @property
    def up(self) -> list[Example]:
        return [e for e in self.judgments if e.vote is Vote.UP]

    @property
    def down(self) -> list[Example]:
        return [e for e in self.judgments if e.vote is Vote.DOWN]

    @property
    def up_agreed(self) -> list[Example]:
        return self._with(Vote.UP, True)

    @property
    def up_missed(self) -> list[Example]:
        """👍 on jobs Claude scored below the threshold (the digest would have hidden them)."""
        return self._with(Vote.UP, False)

    @property
    def down_agreed(self) -> list[Example]:
        return self._with(Vote.DOWN, False)

    @property
    def down_wrong(self) -> list[Example]:
        """👎 on jobs Claude put in the digest."""
        return self._with(Vote.DOWN, True)

    @property
    def unscored(self) -> list[Example]:
        return [e for e in self.judgments if e.overall_score is None]

    @property
    def agreement(self) -> float | None:
        judged = len(self.judgments) - len(self.unscored)
        agreed = len(self.up_agreed) + len(self.down_agreed)
        return agreed / judged if judged else None

    @staticmethod
    def mean_score(examples: list[Example]) -> float | None:
        scores = [e.overall_score for e in examples if e.overall_score is not None]
        return mean(scores) if scores else None

    @property
    def disagreements(self) -> list[Example]:
        return [e for e in self.judgments if e.disagrees(self.min_score)]


def feedback_stats(
    session: Session, min_score: int, since: datetime | None = None
) -> FeedbackStats:
    judgments = load_judgments(session, limit=100_000)
    if since is not None:
        judgments = [e for e in judgments if e.updated_at >= since]
    return FeedbackStats(min_score=min_score, judgments=judgments)
