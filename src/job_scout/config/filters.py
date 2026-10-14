"""Code-side pre-filters (config/filters.yaml). All patterns are case-insensitive regexes."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import AfterValidator, Field, PositiveInt

from job_scout.config.settings import StrictModel


def _compiles(pattern: str) -> str:
    try:
        re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"invalid regex {pattern!r}: {exc}") from exc
    return pattern


Regex = Annotated[str, AfterValidator(_compiles)]

# Job columns a dedupe key may be built from.
DedupeField = Literal[
    "company_normalized", "title_normalized", "canonical_url", "apply_url", "location_raw"
]


class TitleFilter(StrictModel):
    include_any: list[Regex] = Field(min_length=1)
    exclude_any: list[Regex] = []


class RemoteFilter(StrictModel):
    exclude_any: list[Regex] = []


class LocationFilter(StrictModel):
    allow_any: list[Regex] = []
    exclude_any: list[Regex] = []
    on_unclear: Literal["pass_to_llm"] = "pass_to_llm"


class SalaryFilter(StrictModel):
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    min_annual: PositiveInt
    hours_per_year: PositiveInt = 2080
    months_per_year: PositiveInt = 12
    rule: Literal["drop_if_max_below_min"] = "drop_if_max_below_min"
    on_unlisted: Literal["pass_to_llm"] = "pass_to_llm"


class DedupeConfig(StrictModel):
    key: list[DedupeField] = Field(min_length=1)


class FiltersConfig(StrictModel):
    titles: TitleFilter
    remote: RemoteFilter
    location: LocationFilter
    salary: SalaryFilter
    dedupe: DedupeConfig
