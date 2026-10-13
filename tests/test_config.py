import hashlib
from pathlib import Path

import pytest

from job_scout.config import ConfigError, load_config, load_settings_only
from tests.conftest import REPO_CONFIG, edit


def test_repo_config_is_valid() -> None:
    config = load_config(REPO_CONFIG)
    assert config.settings.scoring.backend == "cli"
    assert config.settings.scoring.model == "claude-haiku-5-5"
    assert (
        config.profile.sha256
        == hashlib.sha256((REPO_CONFIG / "profile.md").read_bytes()).hexdigest()
    )
    assert {"overall_score", "location_eligibility"} <= config.profile.output_example.keys()
    assert config.sources.watchlist[0].company == "Canonical"


def test_only_tier_1_enabled_by_default() -> None:
    sources = load_config(REPO_CONFIG).sources
    enabled = sources.enabled()
    assert {s.tier for s in enabled} == {1}
    assert "remoteok" in {s.id for s in enabled}
    assert sources.enabled(tier=2) == []


def test_enabled_flag_overrides_tier(config_dir: Path) -> None:
    edit(config_dir / "sources.yaml", "  - id: nodesk\n", "  - id: nodesk\n    enabled: true\n")
    edit(config_dir / "sources.yaml", "  - id: pyjobs\n", "  - id: pyjobs\n    enabled: false\n")
    ids = {s.id for s in load_config(config_dir).sources.enabled()}
    assert "nodesk" in ids
    assert "pyjobs" not in ids


def test_dedupe_key_has_no_url() -> None:
    assert load_config(REPO_CONFIG).filters.dedupe.key == ["company_normalized", "title_normalized"]


def test_skipped_reasons_with_commas_are_whole() -> None:
    skipped = {s.name: s.reason for s in load_config(REPO_CONFIG).sources.skipped}
    assert skipped["FoundRole"] == "job-search tool, not a job board"


# --- settings precedence: flag > env (short alias > nested) > file > default ---


def test_file_beats_default(config_dir: Path) -> None:
    edit(config_dir / "settings.yaml", "min_score: 50", "min_score: 70")
    assert load_settings_only(config_dir).digest.min_score == 70


def test_default_used_when_file_omits_value(config_dir: Path) -> None:
    edit(config_dir / "settings.yaml", "  order: newest_first", "")
    assert load_settings_only(config_dir).scoring.order == "newest_first"


def test_env_beats_file(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOB_SCOUT_SCORING__BACKEND", "api")
    monkeypatch.setenv("JOB_SCOUT_SCORING__MODES__DAILY__CLI__MAX_CALLS", "3")
    settings = load_settings_only(config_dir)
    assert settings.scoring.backend == "api"
    # A nested env override changes one value and keeps its siblings from the file.
    assert settings.scoring.modes.daily.cli.max_calls == 3
    assert settings.scoring.modes.daily.cli.postings_per_call == 20
    assert settings.scoring.modes.backlog.cli.max_calls == 25


def test_short_env_alias_beats_nested_env(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JOB_SCOUT_SCORING__MODEL", "from-nested")
    monkeypatch.setenv("JOB_SCOUT_MODEL", "from-alias")
    monkeypatch.setenv("JOB_SCOUT_BACKEND", "api")
    settings = load_settings_only(config_dir)
    assert settings.scoring.model == "from-alias"
    assert settings.scoring.backend == "api"


def test_flag_beats_env(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOB_SCOUT_BACKEND", "api")
    monkeypatch.setenv("JOB_SCOUT_MODEL", "from-env")
    settings = load_settings_only(config_dir, {"scoring": {"backend": "cli"}})
    assert settings.scoring.backend == "cli"
    assert settings.scoring.model == "from-env"  # untouched by the flag override


def test_database_url(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert load_settings_only(config_dir).db_url == "sqlite:////var/lib/job-scout/job-scout.db"
    monkeypatch.setenv("JOB_SCOUT_PATHS__DATABASE", "data/x.db")
    assert load_settings_only(config_dir).db_url == "sqlite:///data/x.db"
    monkeypatch.setenv("JOB_SCOUT_DATABASE_URL", "postgresql+psycopg://h/db")
    assert load_settings_only(config_dir).db_url == "postgresql+psycopg://h/db"


def test_limits_with_flag_overrides() -> None:
    scoring = load_settings_only(REPO_CONFIG).scoring
    limits = scoring.limits("backlog", "cli", postings_per_call=60, max_calls=None)
    assert (limits.postings_per_call, limits.max_calls) == (60, 25)
    assert scoring.limits("backlog", "api").use_batches_api is True
    with pytest.raises(ValueError, match="max_input_tokens_per_call"):
        scoring.limits("daily", "api", max_input_tokens_per_call=1000)


# --- typos and bad values fail loudly ---


@pytest.mark.parametrize(
    ("file", "old", "new", "message"),
    [
        ("settings.yaml", "min_score: 50", "min_scor: 50", "digest.min_scor"),
        ("settings.yaml", "backend: cli", "backend: gpt", "scoring.backend"),
        ("settings.yaml", "timezone: America/Guatemala", "timezone: Mars/Base", "unknown timezone"),
        ("sources.yaml", "    tier: 1\n", "    tier: 1\n    teir: 1\n", "teir"),
        ("sources.yaml", "method: rss", "method: carrier-pigeon", "method"),
        ("sources.yaml", "ats: greenhouse", "ats: taleo", "unknown ats"),
        ("sources.yaml", "id: pyjobs", "id: remoteok", "duplicate source id"),
        ("filters.yaml", "'\\bprogrammer\\b'", "'(unclosed'", "invalid regex"),
        ("filters.yaml", "min_annual: 120000", "min_anual: 120000", "min_anual"),
        ("filters.yaml", "key: [company_normalized", "key: [company_name", "dedupe.key"),
        ("profile.md", "## Output format", "## Output", "Output format"),
        ("profile.md", '"overall_score": 0,', "", "overall_score"),
    ],
)
def test_invalid_config_fails(
    config_dir: Path, file: str, old: str, new: str, message: str
) -> None:
    edit(config_dir / file, old, new)
    with pytest.raises(ConfigError, match=message) as excinfo:
        load_config(config_dir)
    assert file in str(excinfo.value)


def test_missing_file_fails(config_dir: Path) -> None:
    (config_dir / "filters.yaml").unlink()
    with pytest.raises(ConfigError, match=r"filters\.yaml"):
        load_config(config_dir)
