"""The profile skill's references stay valid as the code changes."""

import re

import pytest
import yaml

from job_scout.config import load_points
from job_scout.config.filters import FiltersConfig
from job_scout.config.profile import load_profile
from job_scout.scoring.points import check_points
from job_scout.scoring.schema import score_schema
from tests.conftest import ROOT

SKILL = ROOT / ".claude" / "skills" / "job-scout-profile"


def test_skill_has_a_name_and_description() -> None:
    front = re.match(r"^---\n(.*?)\n---\n", (SKILL / "SKILL.md").read_text(), re.DOTALL)
    assert front, "SKILL.md needs YAML frontmatter"
    meta = yaml.safe_load(front.group(1))
    assert meta["name"] == "job-scout-profile" and len(meta["description"]) > 50


def test_profile_template_declares_the_core_fields() -> None:
    profile = load_profile(SKILL / "references" / "profile-template.md")
    fields = {f.name for f in profile.output_fields}
    assert {"travel_required", "contract_length", "contract_type"} <= fields
    score_schema(profile)  # core fields declared as the code types them
    check_points(load_points(SKILL / "references" / "scoring-template.yaml"), profile)


def example_filters() -> FiltersConfig:
    text = (SKILL / "references" / "filters-guide.md").read_text()
    block = re.search(r"^```yaml\n(.*?)^```", text, re.DOTALL | re.MULTILINE)
    assert block
    return FiltersConfig.model_validate(yaml.safe_load(block.group(1)))


@pytest.mark.parametrize(
    ("title", "kept"),
    [
        ("Program Director, Central America", True),
        ("Country Director - Guatemala", True),
        ("Deputy Chief of Party", True),
        ("MEL Specialist (home-based)", True),
        ("Directora de Programa", True),
        ("Coordinador de Proyectos Regional", True),
        ("Consultoría para evaluación final", True),
        ("Program Assistant", False),
        ("Pasantía en comunicación", False),
        ("Senior Backend Engineer", False),
    ],
)
def test_example_filters_on_titles(title: str, kept: bool) -> None:
    filters = example_filters()

    def hit(patterns: list[str]) -> bool:
        return any(re.search(p, title, re.IGNORECASE) for p in patterns)

    assert (hit(filters.titles.include_any) and not hit(filters.titles.exclude_any)) is kept
