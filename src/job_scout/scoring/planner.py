"""Pack pending postings into calls using the active mode's limits (PLAN.md §7.2)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from job_scout.config.settings import ApiLimits, CliLimits
from job_scout.scoring.prompt import OUTPUT_TOKENS_PER_POSTING, PostingInput


@dataclass(frozen=True)
class Limits:
    postings_per_call: int
    max_calls: int
    max_input_tokens: int | None
    max_output_tokens: int | None

    @classmethod
    def from_settings(cls, limits: CliLimits | ApiLimits) -> Limits:
        if isinstance(limits, CliLimits):
            return cls(
                limits.postings_per_call,
                limits.max_calls,
                limits.max_input_tokens_per_call,
                limits.max_output_tokens_per_call,
            )
        return cls(limits.postings_per_call, limits.max_calls, None, None)


class BatchPlanner:
    """Fill each call until postings-per-call, the input budget or the output budget is hit.

    Long descriptions therefore produce smaller calls automatically. `system_tokens` counts
    against the input budget of every call. A posting too large for an empty call still
    gets a call of its own rather than being skipped.
    """

    def __init__(self, limits: Limits, system_tokens: int) -> None:
        self.limits = limits
        self.system_tokens = system_tokens

    def plan(
        self, postings: Sequence[PostingInput], max_calls: int | None = None
    ) -> list[list[PostingInput]]:
        calls: list[list[PostingInput]] = []
        current: list[PostingInput] = []
        input_tokens = self.system_tokens
        cap = self.limits.max_calls if max_calls is None else max_calls
        for posting in postings:
            if current and not self._fits(current, input_tokens, posting):
                calls.append(current)
                if len(calls) >= cap:
                    return calls
                current, input_tokens = [], self.system_tokens
            current.append(posting)
            input_tokens += posting.estimated_tokens
        if current and len(calls) < cap:
            calls.append(current)
        return calls

    def _fits(self, current: list[PostingInput], input_tokens: int, posting: PostingInput) -> bool:
        limits = self.limits
        count = len(current) + 1
        within_count = count <= limits.postings_per_call
        within_input = (
            not limits.max_input_tokens
            or input_tokens + posting.estimated_tokens <= limits.max_input_tokens
        )
        within_output = (
            not limits.max_output_tokens
            or count * OUTPUT_TOKENS_PER_POSTING <= limits.max_output_tokens
        )
        return within_count and within_input and within_output
