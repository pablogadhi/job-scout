"""Loading and validation of the four files in config/. Any typo fails loudly at startup."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from job_scout.config.filters import FiltersConfig
from job_scout.config.profile import Profile, ProfileError, load_profile
from job_scout.config.settings import Settings, load_settings
from job_scout.config.sources import SourceConfig, SourcesConfig

__all__ = [
    "AppConfig",
    "ConfigError",
    "FiltersConfig",
    "Profile",
    "Settings",
    "SourceConfig",
    "SourcesConfig",
    "load_config",
    "load_settings_only",
]

DEFAULT_CONFIG_DIR = Path("config")


class ConfigError(Exception):
    """A config file is missing or invalid. The message names the file."""


@dataclass(frozen=True)
class AppConfig:
    config_dir: Path
    settings: Settings
    sources: SourcesConfig
    filters: FiltersConfig
    profile: Profile


def _describe(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        lines = [
            f"  {'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            + (f" (got {err['input']!r})" if err["type"] != "missing" else "")
            for err in exc.errors(include_url=False)
        ]
        return f"{exc.error_count()} validation error(s)\n" + "\n".join(lines)
    return str(exc)


def _load_yaml_model[M: BaseModel](model: type[M], path: Path) -> M:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return model.model_validate(data)
    except FileNotFoundError as exc:
        raise ConfigError(f"{path}: file not found") from exc
    except (yaml.YAMLError, ValidationError) as exc:
        raise ConfigError(f"{path}: {_describe(exc)}") from exc


def load_settings_only(
    config_dir: Path = DEFAULT_CONFIG_DIR, overrides: dict[str, Any] | None = None
) -> Settings:
    path = config_dir / "settings.yaml"
    try:
        return load_settings(path, overrides)
    except (FileNotFoundError, yaml.YAMLError, ValidationError) as exc:
        # Settings merge file, env vars and flags, so the bad value may come from any of them.
        raise ConfigError(f"{path} / JOB_SCOUT_* env / flags: {_describe(exc)}") from exc


def load_config(
    config_dir: Path = DEFAULT_CONFIG_DIR, overrides: dict[str, Any] | None = None
) -> AppConfig:
    """Load and validate settings.yaml, sources.yaml, filters.yaml and profile.md."""
    profile_path = config_dir / "profile.md"
    try:
        profile = load_profile(profile_path)
    except (OSError, UnicodeDecodeError, ProfileError) as exc:
        raise ConfigError(f"{profile_path}: {exc}") from exc
    return AppConfig(
        config_dir=config_dir,
        settings=load_settings_only(config_dir, overrides),
        sources=_load_yaml_model(SourcesConfig, config_dir / "sources.yaml"),
        filters=_load_yaml_model(FiltersConfig, config_dir / "filters.yaml"),
        profile=profile,
    )
