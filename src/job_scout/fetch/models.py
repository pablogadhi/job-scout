"""The normalized shape every source mapper produces, validated before it is stored."""

from __future__ import annotations

from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from job_scout.db.models import SalaryPeriod
from job_scout.fetch.normalize import Salary


class Posting(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_job_id: str = Field(min_length=1, max_length=255)
    canonical_url: str = Field(min_length=1)
    apply_url: str | None = None
    company: str | None = None
    title: str = Field(min_length=1, max_length=500)
    location_raw: str | None = None
    remote_raw: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = Field(None, pattern=r"^[A-Z]{3}$")
    salary_period: SalaryPeriod | None = None
    description_text: str | None = None
    tags: list[str] = []
    posted_at: AwareDatetime | None = None
    raw: dict[str, Any]

    @field_validator("company", "location_raw", "remote_raw", "apply_url", "description_text")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        return value.strip() or None if value else None

    @field_validator("title")
    @classmethod
    def _strip_title(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(t.strip() for t in value if t and t.strip()))

    def with_salary(self, salary: Salary | None) -> Posting:
        if salary is None:
            return self
        return Posting.model_validate(
            {
                **self.model_dump(),
                "salary_min": salary.min,
                "salary_max": salary.max,
                "salary_currency": salary.currency,
                "salary_period": salary.period,
            }
        )
