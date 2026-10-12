from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from job_scout.db.models import Backend

CallStatusName = Literal["ok", "usage_limit", "refusal", "truncated", "invalid", "error", "timeout"]


@dataclass(frozen=True)
class CallRequest:
    system: str
    user: str
    job_ids: tuple[int, ...]
    schema: dict[str, Any]
    max_output_tokens: int


@dataclass
class CallResult:
    status: CallStatusName
    payload: Any = None  # the parsed {"results": [...]} object when status == "ok"
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    duration_ms: int | None = None
    exit_code: int | None = None
    http_status: int | None = None
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class Scorer(Protocol):
    backend: Backend
    model: str

    def score(self, request: CallRequest) -> CallResult: ...
