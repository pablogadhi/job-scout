"""Runtime settings (config/settings.yaml).

Precedence for every value: CLI flag > environment variable > settings.yaml > built-in default.

Environment variables use the JOB_SCOUT_ prefix with `__` between nesting levels
(e.g. JOB_SCOUT_SCORING__MODES__DAILY__CLI__MAX_CALLS=3). The documented short forms
JOB_SCOUT_BACKEND and JOB_SCOUT_MODEL are also accepted and win over the long forms.
CLI flags arrive as a nested dict of overrides passed to `load_settings`.
"""

from __future__ import annotations

import os
from datetime import date, time
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    field_validator,
    model_validator,
)
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from job_scout.db.models import Backend, ScoringMode

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]

ENV_PREFIX = "JOB_SCOUT_"

# Short env var names documented in PLAN.md, mapped to their nested setting.
ENV_ALIASES: dict[str, tuple[str, ...]] = {
    "JOB_SCOUT_BACKEND": ("scoring", "backend"),
    "JOB_SCOUT_MODEL": ("scoring", "model"),
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CliLimits(StrictModel):
    postings_per_call: PositiveInt
    max_calls: PositiveInt
    max_input_tokens_per_call: PositiveInt
    max_output_tokens_per_call: PositiveInt


class ApiLimits(StrictModel):
    postings_per_call: PositiveInt
    max_calls: PositiveInt
    use_batches_api: bool = False


class ModeLimits(StrictModel):
    cli: CliLimits
    api: ApiLimits


class Modes(StrictModel):
    daily: ModeLimits = ModeLimits(
        cli=CliLimits(
            postings_per_call=20,
            max_calls=5,
            max_input_tokens_per_call=60_000,
            max_output_tokens_per_call=12_000,
        ),
        api=ApiLimits(postings_per_call=1, max_calls=500, use_batches_api=False),
    )
    backlog: ModeLimits = ModeLimits(
        cli=CliLimits(
            postings_per_call=20,
            max_calls=25,
            max_input_tokens_per_call=60_000,
            max_output_tokens_per_call=12_000,
        ),
        api=ApiLimits(postings_per_call=1, max_calls=10_000, use_batches_api=True),
    )


class CliOptions(StrictModel):
    binary: str = "claude"
    workdir: Path = Path("data/claude-cwd")
    timeout_seconds: PositiveInt = 600
    token_created: date | None = None


class ModelPrices(StrictModel):
    """USD per million tokens."""

    input: PositiveFloat
    output: PositiveFloat
    cache_write: PositiveFloat
    cache_read: PositiveFloat


class ApiOptions(StrictModel):
    prompt_caching: bool = True
    max_spend_usd_per_run: PositiveFloat = 5.0
    max_retries: NonNegativeInt = 3
    batch_poll_seconds: PositiveInt = 60
    batch_max_wait_minutes: PositiveInt = 120
    batch_discount: float = Field(default=0.5, gt=0, le=1)
    # Added to max_tokens on every request: room for thinking on models that think by
    # default (Claude Haiku 5.5), which counts toward max_tokens. Unused tokens cost nothing.
    thinking_headroom_tokens: NonNegativeInt = 8_000
    prices: dict[str, ModelPrices] = {
        "claude-haiku-4-5": ModelPrices(input=1.0, output=5.0, cache_write=1.25, cache_read=0.1),
        "claude-haiku-5-5": ModelPrices(
            input=0.10, output=0.50, cache_write=0.125, cache_read=0.01
        ),
    }


Effort = Literal["low", "medium", "high", "xhigh", "max"]


class Scoring(StrictModel):
    backend: Backend = Backend.CLI
    model: str = "claude-haiku-5-5"
    # None: don't send it (Haiku 4.5 rejects it; Haiku 5.5 then defaults to medium).
    effort: Effort | None = None
    description_max_chars: PositiveInt = 6000
    modes: Modes = Modes()
    order: Literal["newest_first", "oldest_first"] = "newest_first"
    cli: CliOptions = CliOptions()
    api: ApiOptions = ApiOptions()

    def limits(
        self, mode: ScoringMode, backend: Backend | None = None, **overrides: Any
    ) -> CliLimits | ApiLimits:
        """Batching limits for a mode and backend, with per-run flag overrides applied.

        Overrides set to None are ignored, so CLI options can be passed straight through.
        """
        limits = getattr(getattr(self.modes, mode), backend or self.backend)
        update = {k: v for k, v in overrides.items() if v is not None}
        unknown = set(update) - set(type(limits).model_fields)
        if unknown:
            raise ValueError(
                f"{', '.join(sorted(unknown))} not valid for the {backend or self.backend} backend"
            )
        return type(limits).model_validate({**limits.model_dump(), **update})


class Email(StrictModel):
    """SMTP delivery. The password (a Proton SMTP token) comes only from JOB_SCOUT_SMTP_TOKEN."""

    enabled: bool = False
    smtp_host: str = "smtp.protonmail.ch"
    smtp_port: PositiveInt = 587  # STARTTLS
    from_address: str | None = None  # also the SMTP username
    # Comma-separated recipients; usually set with JOB_SCOUT_DIGEST__EMAIL__TO_ADDRESS. Checked
    # when sending (like the token), so a machine without it still loads the config.
    to_address: str | None = None
    timeout_seconds: PositiveInt = 30

    @model_validator(mode="after")
    def _sender_when_enabled(self) -> Email:
        if self.enabled and not self.from_address:
            raise ValueError("email.enabled needs from_address")
        return self

    @property
    def recipients(self) -> list[str]:
        return [a.strip() for a in (self.to_address or "").split(",") if a.strip()]


class Digest(StrictModel):
    """The HTML file is always written (paths.reports); email is optional."""

    min_score: int = Field(default=60, ge=0, le=100)
    watchlist_min_score: int = Field(default=21, ge=0, le=100)
    # Watchlist jobs scoring below this are listed collapsed (one line each in plain text), so
    # a watchlist company posting many jobs at once doesn't swamp the report.
    watchlist_collapse_below: int = Field(default=50, ge=0, le=100)
    send_when_empty: bool = True  # email a digest with no new matches (its health footer)
    email: Email = Email()


class Schedule(StrictModel):
    timezone: str = "America/Guatemala"
    daily_run_time: time = time(8, 15)  # the systemd timer (deploy/systemd) must match

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value


class Ats(StrictModel):
    """Company job boards (remoteintech seed + watchlist; sources.yaml ats_platforms)."""

    seed_refresh_days: PositiveInt = 7  # re-download the seed list this often
    redetect_days: PositiveInt = 30  # re-detect a company's board this often
    # `run` detects at most this many new or stale companies (`ats detect` has no cap).
    max_detections_per_run: NonNegativeInt = 40


class Feedback(StrictModel):
    """Thumbs up/down from the digest (`job-scout feedback serve`) and judgments as examples.

    The link-signing secret comes only from JOB_SCOUT_FEEDBACK_SECRET, never from this file.
    """

    # Where the digest's links point, e.g. http://jobscout.lan:8765. None: no links. It differs
    # per machine, so it's set with JOB_SCOUT_FEEDBACK__BASE_URL rather than in settings.yaml.
    base_url: str | None = None
    # The page's listening address. The compose service sets 0.0.0.0 inside the container.
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    # Recent judgments added to the scoring prompt as examples (0: none).
    prompt_examples: NonNegativeInt = 8

    @field_validator("base_url")
    @classmethod
    def _http_base_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        if not value.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        return value.strip().rstrip("/")


class Paths(StrictModel):
    """Relative paths resolve against the working directory: the repo root, or /app in the
    container, where the repo's data/ is mounted."""

    database: Path = Path("data/job-scout.db")
    reports: Path = Path("data/reports")
    exports: Path = Path("data/exports")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        extra="forbid",
        env_prefix=ENV_PREFIX,
        env_nested_delimiter="__",
        nested_model_default_partial_update=True,
    )

    scoring: Scoring = Scoring()
    digest: Digest = Digest()
    ats: Ats = Ats()
    feedback: Feedback = Feedback()
    schedule: Schedule = Schedule()
    paths: Paths = Paths()
    # Full SQLAlchemy URL; overrides paths.database (e.g. to move to Postgres).
    database_url: str | None = None
    log_level: LogLevel = "INFO"

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite:///{self.paths.database}"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Highest priority first. init_settings carries CLI flag overrides.
        return (
            init_settings,
            _EnvAliasSource(settings_cls),
            env_settings,
            YamlConfigSettingsSource(settings_cls),
        )


class _EnvAliasSource(PydanticBaseSettingsSource):
    """Maps the short env var names in ENV_ALIASES onto their nested settings."""

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for var, path in ENV_ALIASES.items():
            if (value := os.environ.get(var)) is None:
                continue
            node = values
            for key in path[:-1]:
                node = node.setdefault(key, {})
            node[path[-1]] = value
        return values


def load_settings(path: Path, overrides: dict[str, Any] | None = None) -> Settings:
    """Load settings from `path`, applying env vars and CLI `overrides` on top."""
    if not path.is_file():
        raise FileNotFoundError(f"settings file not found: {path}")

    class FileSettings(Settings):
        model_config = SettingsConfigDict(yaml_file=path)

    return FileSettings(**(overrides or {}))
