"""Claude's structured output: the schema in profile.md's "Output format" plus `job_id`.

One schema for both backends and any batch size: `{"results": [ScoreResult, ...]}`.
"""

from __future__ import annotations

import copy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from job_scout.config.profile import Profile

RoleCategory = Literal[
    "backend",
    "full-stack",
    "frontend",
    "infrastructure",
    "devops",
    "platform",
    "sre",
    "tech-lead",
    "data-engineering",
    "iot",
    "embedded",
    "engineering-manager",
    "mobile",
    "other",
]


class ScoreResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: int
    disqualified: bool
    disqualify_reason: str | None
    remote: Literal["yes", "no", "unclear"]
    location_eligibility: Literal["pass", "fail", "unclear"]
    location_notes: str
    salary: Literal["meets", "below", "unlisted"]
    salary_usd_range: str | None
    salary_notes: str
    role_category: RoleCategory
    seniority: str
    skill_fit: int = Field(ge=0, le=5)
    seniority_fit: int = Field(ge=0, le=5)
    matched_skills: list[str]
    gaps: list[str]
    four_day_week: Literal["yes", "no", "unknown"]
    wlb_positive: list[str]
    wlb_negative: list[str]
    employment_type: Literal["employee", "eor", "contractor", "unknown"]
    red_flags: list[str]
    overall_score: int = Field(ge=0, le=100)
    summary: str


class ScoreBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    results: list[ScoreResult]


class SchemaMismatch(ValueError):
    pass


def check_profile_matches(profile: Profile) -> None:
    """Fail loudly if profile.md's output format and ScoreResult drift apart."""
    expected = set(profile.output_example)
    actual = set(ScoreResult.model_fields) - {"job_id"}
    if expected != actual:
        raise SchemaMismatch(
            "profile.md output format and ScoreResult differ: "
            f"only in profile.md: {sorted(expected - actual)}; "
            f"only in ScoreResult: {sorted(actual - expected)}"
        )


# Keywords structured outputs rejects; Pydantic still enforces them when parsing the reply.
_UNSUPPORTED = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
                "minLength", "maxLength", "title", "default"}  # fmt: skip


def _strip(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            # Keys under "properties"/"$defs" are names, not keywords: never drop them.
            k: {name: _strip(sub) for name, sub in v.items()}
            if k in ("properties", "$defs")
            else _strip(v)
            for k, v in node.items()
            if k not in _UNSUPPORTED
        }
    if isinstance(node, list):
        return [_strip(v) for v in node]
    return node


def output_json_schema() -> dict[str, Any]:
    """JSON schema for `{"results": [...]}` that both the API and the CLI accept."""
    return _strip(copy.deepcopy(ScoreBatch.model_json_schema()))
