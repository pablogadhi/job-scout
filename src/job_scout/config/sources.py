"""Job sources, skipped sites, ATS templates and the watchlist (config/sources.yaml)."""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from pydantic import Field, PositiveFloat, PositiveInt, field_validator, model_validator

from job_scout.config.settings import StrictModel

Method = Literal["api", "rss", "scrape", "ats_seed"]
Checked = date | Literal["unverified"]


class SourceDefaults(StrictModel):
    user_agent: str
    respect_robots_txt: bool = True
    min_interval_hours: PositiveFloat = 6
    timeout_seconds: PositiveFloat = 30


class SourceConfig(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    name: str
    board_list: Literal["job_boards", "aggregators"] = Field(alias="list")
    url: str
    method: Method
    tier: Literal[1, 2, 3]
    checked: Checked
    # Not set in sources.yaml today: tier 1 is on, tiers 2 and 3 are opt-in.
    enabled: bool | None = None
    endpoints: list[str] = []
    format: Literal["json", "rss"] | None = None
    pagination: str | None = None
    useful_params: list[str] = []
    alt_api: str | None = None
    auth: str | None = None
    # Env var holding the API key; the source is skipped (not failed) while it is unset.
    auth_env: str | None = Field(None, pattern=r"^[A-Z][A-Z0-9_]*$")
    # Enforced limits; free-text limits in `notes` are documentation only.
    max_requests_per_day: PositiveInt | None = None  # rolling 24 hours
    min_request_interval_seconds: PositiveFloat | None = None
    notes: str | None = None
    terms: str | None = None

    @property
    def is_enabled(self) -> bool:
        return self.enabled if self.enabled is not None else self.tier <= 1


class SkippedSite(StrictModel):
    name: str
    url: str
    reason: str


class AtsPlatform(StrictModel):
    detect: str
    api: str

    @field_validator("detect")
    @classmethod
    def _valid_regex(cls, value: str) -> str:
        re.compile(value)
        return value

    @field_validator("api")
    @classmethod
    def _one_placeholder(cls, value: str) -> str:
        if len(re.findall(r"\{\w+\}", value)) != 1:
            raise ValueError(f"API template needs exactly one {{placeholder}}: {value}")
        return value


class WatchlistEntry(StrictModel):
    company: str
    careers_url: str
    ats: str
    token: str
    checked: Checked
    notes: str | None = None


class SourcesConfig(StrictModel):
    defaults: SourceDefaults
    sources: list[SourceConfig]
    skipped: list[SkippedSite] = []
    ats_platforms: dict[str, AtsPlatform] = {}
    watchlist: list[WatchlistEntry] = []

    @model_validator(mode="after")
    def _cross_checks(self) -> SourcesConfig:
        seen: set[str] = set()
        for source in self.sources:
            if source.id in seen:
                raise ValueError(f"duplicate source id: {source.id}")
            seen.add(source.id)
        for entry in self.watchlist:
            if entry.ats not in self.ats_platforms:
                raise ValueError(
                    f"watchlist entry {entry.company!r} uses unknown ats {entry.ats!r}; "
                    f"known: {', '.join(self.ats_platforms)}"
                )
        return self

    def enabled(self, tier: int | None = None) -> list[SourceConfig]:
        return [s for s in self.sources if s.is_enabled and (tier is None or s.tier == tier)]

    def get(self, source_id: str) -> SourceConfig:
        for source in self.sources:
            if source.id == source_id:
                return source
        raise KeyError(source_id)
