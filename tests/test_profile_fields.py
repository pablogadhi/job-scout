"""A profile with its own output fields, end to end: the scoring schema, validation and
storage, the reports and the CSV export. Synthetic profile and postings only."""

import csv
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from job_scout.config import AppConfig, load_config
from job_scout.config.profile import OutputField, ProfileError, load_profile, output_field
from job_scout.db.models import Score, ScoreStatus, ScoringMode
from job_scout.report.build import make_backlog_report
from job_scout.report.data import extra_output_fields
from job_scout.report.export import export_csv
from job_scout.scoring.backends.base import CallRequest, CallResult
from job_scout.scoring.planner import Limits
from job_scout.scoring.runner import RunOptions, ScoringRunner
from tests.scoring_helpers import FakeScorer, seed_jobs

NOW = datetime(2026, 10, 15, 14, 0, tzinfo=UTC)

# A nonprofit program profile: the core fields, its own role categories, three fields of
# its own, and none of the first profile's skill fit, 4-day week or work-life fields.
OUTPUT_FORMAT = {
    "disqualified": False,
    "disqualify_reason": None,
    "remote": "yes | no | unclear",
    "location_eligibility": "pass | fail | unclear",
    "location_notes": "string",
    "salary": "meets | below | unlisted",
    "salary_notes": "string",
    "role_category": "program-director | program-manager | consultant | other",
    "seniority": "string as stated in the posting",
    "travel_required": "none | occasional | frequent | unknown",
    "contract_length": "e.g. 6 months, or null",
    "donors": ["string"],
    "matched_skills": ["string"],
    "gaps": ["string"],
    "red_flags": ["string"],
    "overall_score": 0,
    "summary": "One sentence on the fit.",
}


def program_result(job_id: int, **overrides: Any) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "disqualified": False,
        "disqualify_reason": None,
        "remote": "yes",
        "location_eligibility": "pass",
        "location_notes": "Home-based, open to Central America.",
        "salary": "unlisted",
        "salary_notes": "",
        "role_category": "program-director",
        "seniority": "Director",
        "travel_required": "frequent",
        "contract_length": "6 months",
        "donors": ["IDB", "EU"],
        "matched_skills": ["Program design"],
        "gaps": [],
        "red_flags": [],
        "overall_score": 80,
        "summary": "Synthetic program role that fits.",
        **overrides,
    }


@pytest.fixture
def program_config(config_dir: Path, tmp_path: Path) -> AppConfig:
    profile = config_dir / "profile.md"
    text = profile.read_text()
    block = re.compile(r"(^## Output format\b.*?^```json\n)(.*?)(^```)", re.DOTALL | re.MULTILINE)
    profile.write_text(
        block.sub(lambda m: m[1] + json.dumps(OUTPUT_FORMAT, indent=2) + "\n" + m[3], text)
    )
    return load_config(
        config_dir,
        {"paths": {"reports": str(tmp_path / "reports")}, "digest": {"email": {"enabled": False}}},
    )


def test_the_profile_declares_its_own_fields(program_config: AppConfig) -> None:
    fields = {f.name: f for f in program_config.profile.output_fields}
    assert fields["travel_required"] == OutputField(
        "travel_required", "enum", ("none", "occasional", "frequent", "unknown")
    )
    assert fields["contract_length"] == OutputField("contract_length", "str", nullable=True)
    assert fields["donors"].kind == "list"
    assert extra_output_fields(program_config.profile) == [
        "travel_required",
        "contract_length",
        "donors",
    ]


def test_scored_reported_and_exported(
    program_config: AppConfig, session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    good, bad = seed_jobs(session_factory, 2)

    def reply(request: CallRequest) -> CallResult:
        results = [
            program_result(good),
            program_result(bad, travel_required="sometimes"),  # not one of its values
        ]
        return CallResult("ok", payload={"results": results}, input_tokens=1000)

    scorer = FakeScorer(reply)
    limits = Limits(postings_per_call=20, max_calls=1, max_input_tokens=60_000,
                    max_output_tokens=12_000)  # fmt: skip
    ScoringRunner(program_config, session_factory, scorer).run(
        RunOptions(mode=ScoringMode.DAILY, limits=limits)
    )

    # Claude is asked for exactly this profile's fields.
    properties = scorer.requests[0].schema["$defs"]["ScoreResult"]["properties"]
    assert list(properties) == ["job_id", *OUTPUT_FORMAT]
    assert properties["role_category"]["enum"][0] == "program-director"

    with session_factory() as session:
        stored = {s.job_id: s for s in session.scalars(select(Score))}
    assert stored[good].status == ScoreStatus.SCORED
    result = stored[good].result
    assert result is not None and result["donors"] == ["IDB", "EU"]
    assert stored[bad].status == ScoreStatus.PENDING and stored[bad].attempts == 1

    html = make_backlog_report(program_config, session_factory, now=NOW, environ={}).path
    page = html.read_text()
    assert '<span class="label">Travel required:</span> frequent' in page
    assert '<span class="label">Contract length:</span> 6 months' in page
    assert '<span class="label">Donors:</span> IDB, EU' in page
    # The first profile's role row needs fields this one doesn't have: the short form.
    assert '<span class="label">Role:</span> program-director · Director · remote: yes' in page
    assert "4-day week" not in page and "skill fit" not in page

    path = tmp_path / "jobs.csv"
    with session_factory() as session:
        export_csv(session, path, extra_output_fields(program_config.profile))
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    row = next(r for r in rows if r["job_id"] == str(good))
    assert (row["travel_required"], row["contract_length"], row["donors"]) == (
        "frequent",
        "6 months",
        "IDB, EU",
    )


@pytest.mark.parametrize(
    ("example", "expected"),
    [
        ("yes | no", OutputField("f", "enum", ("yes", "no"))),
        ("free text", OutputField("f", "str")),
        ("e.g. 130000-160000, or null", OutputField("f", "str", nullable=True)),
        (None, OutputField("f", "str", nullable=True)),
        (["string"], OutputField("f", "list")),
        ([], OutputField("f", "list")),
        (True, OutputField("f", "bool")),
        (0, OutputField("f", "int")),
        (0.5, OutputField("f", "float")),
    ],
)
def test_field_types_come_from_examples(example: Any, expected: OutputField) -> None:
    assert output_field("f", example) == expected


@pytest.mark.parametrize("example", [{"nested": "object"}, [1, 2], "a | | b", "a | a"])
def test_unsupported_examples_fail_at_load(example: Any) -> None:
    with pytest.raises(ProfileError, match="f:"):
        output_field("f", example)


def test_core_fields_are_required_and_job_id_is_ours(config_dir: Path) -> None:
    profile = config_dir / "profile.md"
    text = profile.read_text()
    profile.write_text(text.replace('  "red_flags": ["string"],\n', "", 1))
    with pytest.raises(ProfileError, match="missing keys: red_flags"):
        load_profile(profile)
    profile.write_text(text.replace('"summary"', '"job_id": 0,\n  "summary"', 1))
    with pytest.raises(ProfileError, match="job_id"):
        load_profile(profile)
