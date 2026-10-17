"""Opt-in live smoke tests: `uv run --env-file .env pytest -m live tests/test_live_scoring.py`.

Each scores 2 synthetic postings with the real backend. They spend: the cli test uses one
Pro prompt (and confirms the subscription accepts the configured Haiku model through the
CLI, per PLAN.md Phase 3; it must fail, never fall back, if it doesn't), the api test a
fraction of a cent.
"""

import os
from pathlib import Path

import pytest

from job_scout.config import AppConfig
from job_scout.db.models import Backend
from job_scout.scoring.backends import make_scorer
from job_scout.scoring.backends.base import CallRequest
from job_scout.scoring.prompt import PostingInput, system_prompt, user_message
from job_scout.scoring.schema import score_schema

pytestmark = pytest.mark.live

POSTINGS = [
    PostingInput(
        1,
        '<posting job_id="1">\nsource: synthetic\ntitle: Senior Backend Engineer (Python/Django)\n'
        "company: Quetzal Labs\nlocation: Remote, Latin America\n"
        "salary: 140,000 - 170,000 USD per year\n"
        "description:\nBuild payment APIs with Django and PostgreSQL on Kubernetes. "
        "Async-first team, 4-day week at full pay.\n</posting>",
    ),
    PostingInput(
        2,
        '<posting job_id="2">\nsource: synthetic\ntitle: Junior iOS Developer\n'
        "company: Brightpath\nlocation: On-site, Berlin\nsalary: not stated\n"
        "description:\nSwift and UIKit. Five days a week in our Berlin office.\n</posting>",
    ),
]


def _score(app_config: AppConfig, backend: Backend, tmp_path: Path) -> None:
    scoring = app_config.settings.scoring
    if backend == Backend.CLI:
        scoring = scoring.model_copy(
            update={"cli": scoring.cli.model_copy(update={"workdir": tmp_path / "claude-cwd"})}
        )
    scorer = make_scorer(scoring, backend, os.environ)
    schema = score_schema(app_config.profile)
    result = scorer.score(
        CallRequest(
            system=system_prompt(app_config.profile),
            user=user_message(POSTINGS),
            job_ids=(1, 2),
            schema=schema.json_schema(),
            max_output_tokens=4_000,
        )
    )
    assert result.status == "ok", f"{backend} backend failed: {result.status}: {result.error}"
    assert isinstance(result.payload, dict)
    parsed = [schema.parse(item) for item in result.payload["results"]]
    assert sorted(data["job_id"] for data, _ in parsed) == [1, 2]
    by_id = {data["job_id"]: core for data, core in parsed}
    assert by_id[1].overall_score > by_id[2].overall_score


@pytest.mark.skipif(
    not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"), reason="CLAUDE_CODE_OAUTH_TOKEN not set"
)
def test_cli_backend_scores_with_haiku(app_config: AppConfig, tmp_path: Path) -> None:
    _score(app_config, Backend.CLI, tmp_path)


@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY not set")
def test_api_backend_scores(app_config: AppConfig, tmp_path: Path) -> None:
    _score(app_config, Backend.API, tmp_path)
