"""The points score: config/scoring.yaml's checks, the formula and its breakdown, and the
recompute of stored scores. Uses the pinned table in tests/fixtures/config/scoring.yaml and
a synthetic output format with the vocabulary it names."""

import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from job_scout.config.points import PointsTable
from job_scout.config.profile import Profile, output_field
from job_scout.db.models import Score, ScoreStatus
from job_scout.scoring import points
from job_scout.scoring.points import (
    Breakdown,
    PointsMismatch,
    check_points,
    compute,
    fingerprint,
    refresh,
)
from tests.conftest import TEST_CONFIG
from tests.scoring_helpers import T0, seed_jobs

TABLE_PATH = TEST_CONFIG / "scoring.yaml"
RAW_TABLE: dict[str, Any] = yaml.safe_load(TABLE_PATH.read_text())
TABLE = PointsTable.model_validate(RAW_TABLE)
SIGNALS = list(RAW_TABLE["components"]["signals"]["points"])

OUTPUT_FORMAT: dict[str, Any] = {
    "remote": "yes | no | unclear",
    "location_eligibility": "pass | fail | unclear",
    "salary": "meets | below | unlisted",
    "role_category": "backend | full-stack | frontend | infrastructure | devops | platform | "
    "sre | tech-lead | data-engineering | iot | embedded | engineering-manager | mobile | other",
    "seniority_level": "intern | junior | mid | senior | lead | staff | principal | unstated",
    "requirements": [
        {
            "skill": "text",
            "importance": "required | nice-to-have",
            "primary": False,
            "match": "core | solid | equivalent | partial | missing",
            "evidence": "short quote",
        }
    ],
    "signals": [{"signal": " | ".join(SIGNALS), "evidence": "short quote"}],
    "summary": "One sentence on the fit.",
}
PROFILE = Profile(
    path=Path("profile.md"),
    text="",
    sha256="points-test",
    output_example=OUTPUT_FORMAT,
    output_fields=tuple(output_field(k, v) for k, v in OUTPUT_FORMAT.items()),
)


def req(skill: str, importance: str, match: str, primary: bool = False) -> dict[str, Any]:
    return {
        "skill": skill,
        "importance": importance,
        "primary": primary,
        "match": match,
        "evidence": f"{skill} experience",
    }


# 6 required (4 core, 1 solid, 1 equivalent) and 3 nice-to-have (1 core, 2 missing):
# share (3 x 5.6 + 1) / 21 = 0.848, so skills 34/40.
GOOD_MIX = [
    req("Python", "required", "core", primary=True),
    req("PostgreSQL", "required", "core"),
    req("Kubernetes", "required", "core"),
    req("Terraform", "required", "core"),
    req("AWS", "required", "solid"),
    req("FastAPI", "required", "equivalent"),
    req("Redis", "nice-to-have", "core"),
    req("Go", "nice-to-have", "missing"),
    req("Rust", "nice-to-have", "missing"),
]


def result(**overrides: Any) -> dict[str, Any]:
    """A senior backend role, eligible, salary unlisted, no signals."""
    return {
        "job_id": 1,
        "remote": "yes",
        "location_eligibility": "pass",
        "salary": "unlisted",
        "role_category": "backend",
        "seniority_level": "senior",
        "requirements": GOOD_MIX,
        "signals": [],
        "summary": "Senior backend role.",
        **overrides,
    }


def signal(name: str) -> dict[str, str]:
    return {"signal": name, "evidence": f"quote for {name}"}


def score_of(**overrides: Any) -> Breakdown:
    breakdown = compute(TABLE, result(**overrides))
    assert breakdown is not None
    return breakdown


# --- the table against the profile -----------------------------------------------------


def test_the_pinned_table_fits_the_output_format() -> None:
    check_points(TABLE, PROFILE)


def _broken(edit: Any) -> PointsTable:
    raw = copy.deepcopy(RAW_TABLE)
    edit(raw)
    return PointsTable.model_validate(raw)


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda t: t["components"]["salary"]["points"].pop("below"), "no points for below"),
        (lambda t: t["components"]["salary"]["points"].update(great=12), "has no value great"),
        (lambda t: t["components"]["salary"].update(field="pay"), "no field 'pay'"),
        (lambda t: t["components"]["salary"].update(field="summary"), "not an 'a | b' field"),
        (lambda t: t["components"]["skills"].update(field="salary"), "not a list of objects"),
        (lambda t: t["components"]["skills"].update(item="name"), "no field 'name'"),
        (
            lambda t: t["components"]["skills"]["match"]["values"].pop("partial"),
            "no points for partial",
        ),
        (
            lambda t: t["components"]["skills"]["primary_missing"].update(subfield="skill"),
            "'skill' is not true/false",
        ),
        (
            lambda t: t["components"]["skills"]["primary_missing"].update(when_match=["absent"]),
            "no match absent",
        ),
        (lambda t: t["components"]["signals"]["points"].pop("hustle"), "no points for hustle"),
        (lambda t: t["components"]["signals"]["points"].update(naps=5), "has no value naps"),
        (lambda t: t["caps"][0]["when"].update(role_category=["designer"]), "no value designer"),
        (lambda t: t["disqualify"][0].update(when={"summary": ["x"]}), "not an 'a | b' field"),
    ],
)
def test_a_table_that_names_what_the_profile_lacks_fails(edit: Any, message: str) -> None:
    with pytest.raises(PointsMismatch, match=message):
        check_points(_broken(edit), PROFILE)


@pytest.mark.parametrize(
    "edit",
    [
        lambda t: t["components"]["salary"].update(pints=1),  # typo
        lambda t: t["components"]["salary"].update(kind="enums"),
        lambda t: t["components"]["skills"]["weight"]["values"].update(required=0),
        lambda t: t["components"]["skills"]["match"]["values"].update(core=1.5),
        lambda t: t["components"]["signals"].update(min=5, max=1),
        lambda t: t["caps"][0].update(max=101),
        lambda t: t.update(components={}),
    ],
)
def test_typos_and_bad_numbers_fail_validation(edit: Any) -> None:
    with pytest.raises(ValidationError):
        _broken(edit)


# --- the formula -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "total"),
    [
        ({}, 80),  # good fit, salary unlisted, no signals
        ({"salary": "meets"}, 84),
        ({"signals": [signal("async_first"), signal("flexible_hours")]}, 87),
        ({"signals": [signal("four_day_week")]}, 92),
        ({"signals": [signal("fast_paced")]}, 76),
        ({"location_eligibility": "unclear"}, 76),
        ({"remote": "unclear"}, 75),
        ({"seniority_level": "staff"}, 75),
        ({"role_category": "data-engineering"}, 70),
        ({"requirements": []}, 66),  # nothing listed counts half
        ({"role_category": "frontend"}, 45),  # 65, capped
        ({"seniority_level": "mid"}, 49),  # 66, capped
        ({"location_eligibility": "fail"}, 20),  # 75, disqualified
    ],
)
def test_archetypes_land_in_their_bands(overrides: dict[str, Any], total: int) -> None:
    assert score_of(**overrides).total == total


def test_a_missing_main_stack_caps_the_skills_and_the_score() -> None:
    go_shop = [req("Go", "required", "missing", primary=True), *GOOD_MIX[1:]]
    breakdown = score_of(requirements=go_shop)
    skills = breakdown.parts[0]
    assert (skills.name, skills.points, skills.max) == ("skills", 10, 40)
    assert (breakdown.subtotal, breakdown.total) == (56, 45)
    assert breakdown.caps == ((45, "main stack missing"),)
    assert skills.items[0].note == "required, missing, primary"
    assert skills.items[0].evidence == "Go experience"


def test_the_breakdown_line_shows_how_the_score_was_reached() -> None:
    good = score_of(signals=[signal("async_first"), signal("flexible_hours")])
    assert good.line() == (
        "skills 34/40 · seniority 20/20 · role 15/15 · salary 6/10 · location 5/5"
        " · signals +7 (async-first +4, flexible hours +3) = 87"
    )
    assert score_of(role_category="frontend").line().endswith("= 65 → 45 (not a target role type)")
    disqualified = score_of(location_eligibility="fail", remote="no")
    assert disqualified.disqualified
    assert disqualified.disqualify_reason == "location not open to Guatemala; not fully remote"
    assert disqualified.line().endswith(
        "= 75 → 20 (disqualified: location not open to Guatemala; not fully remote)"
    )
    assert "remote -5" in score_of(remote="unclear").line()


def test_signals_count_once_and_are_clamped() -> None:
    twice = score_of(signals=[signal("async_first"), signal("async_first")])
    assert twice.parts[-1].points == 4 and len(twice.parts[-1].items) == 1
    awful = [signal(s) for s in ("long_hours", "hustle", "high_intensity", "fast_paced")]
    assert score_of(signals=awful).parts[-1].points == -20  # -23, clamped
    badges = score_of(signals=[signal("four_day_week"), signal("gaming_role_fit")]).badges
    assert badges == ("4-day week", "gaming")


def test_values_the_table_doesnt_know_count_zero_with_a_note() -> None:
    odd = score_of(
        seniority_level="distinguished",
        signals=[signal("naps")],
        requirements=[req("Python", "essential", "core")],
    )
    assert odd.parts[1].points == 0
    assert odd.parts[0].points == 20  # nothing counted: half
    assert odd.notes == (  # in component order
        "requirements: skipped 'Python' (unknown weight or match)",
        "seniority_level: unknown value 'distinguished'",
        "signals: unknown signal 'naps'",
    )


def test_a_result_in_the_old_format_has_no_breakdown() -> None:
    old = {k: v for k, v in result().items() if k not in ("seniority_level", "requirements")}
    assert compute(TABLE, {**old, "overall_score": 72, "skill_fit": 4}) is None


def test_the_breakdown_survives_json_storage() -> None:
    breakdown = score_of(signals=[signal("four_day_week")], role_category="mobile")
    stored = json.loads(json.dumps(breakdown.to_json()))
    assert Breakdown.from_json(stored) == breakdown


def test_rounding_is_half_up() -> None:
    # Nothing listed counts half: 5 x 0.5 = 2.5, which rounds to 3 (round() would give 2).
    table = _broken(lambda t: t["components"]["skills"].update(max=5))
    breakdown = compute(table, result(requirements=[]))
    assert breakdown is not None and breakdown.parts[0].points == 3


def test_the_fingerprint_follows_values_and_the_formula(monkeypatch: pytest.MonkeyPatch) -> None:
    commented = PointsTable.model_validate(yaml.safe_load("# note\n" + TABLE_PATH.read_text()))
    assert fingerprint(commented) == fingerprint(TABLE)
    changed = _broken(lambda t: t["components"]["salary"]["points"].update(meets=9))
    assert fingerprint(changed) != fingerprint(TABLE)
    before = fingerprint(TABLE)
    monkeypatch.setattr(points, "SCORING_VERSION", points.SCORING_VERSION + 1)
    assert fingerprint(TABLE) != before


# --- stored scores ---------------------------------------------------------------------


def _store(sf: sessionmaker[Session], results: dict[int, dict[str, Any] | None]) -> None:
    with sf.begin() as session:
        for job_id, stored in results.items():
            status = ScoreStatus.SCORED if stored is not None else ScoreStatus.PENDING
            session.add(
                Score(
                    job_id=job_id,
                    profile_hash="p",
                    result=stored,
                    overall_score=33,  # Claude's number, from before points
                    disqualified=False,
                    status=status,
                    scored_at=T0 if stored is not None else None,
                )
            )


def test_refresh_recomputes_only_when_the_table_changes(
    session_factory: sessionmaker[Session],
) -> None:
    ids = seed_jobs(session_factory, 3)
    old_format = {"job_id": ids[1], "overall_score": 33, "summary": "old"}
    _store(session_factory, {ids[0]: result(job_id=ids[0]), ids[1]: old_format, ids[2]: None})

    with session_factory.begin() as session:
        assert refresh(session, TABLE) == (1, 1)
    with session_factory() as session:
        rows = {s.job_id: s for s in session.scalars(select(Score))}
        assert rows[ids[0]].overall_score == 80 and rows[ids[0]].breakdown is not None
        assert rows[ids[1]].overall_score == 33 and rows[ids[1]].breakdown is None  # kept
        assert rows[ids[2]].score_fingerprint is None  # pending: left alone
        assert rows[ids[0]].score_fingerprint == fingerprint(TABLE)

    with session_factory.begin() as session:
        assert refresh(session, TABLE) == (0, 0)  # nothing changed: nothing to do

    harsher = _broken(
        lambda t: t["caps"].append(
            {"when": {"salary": ["unlisted"]}, "max": 60, "reason": "salary unlisted"}
        )
    )
    with session_factory.begin() as session:
        assert refresh(session, harsher) == (1, 1)
    with session_factory() as session:
        score = session.scalars(select(Score).where(Score.job_id == ids[0])).one()
        assert score.overall_score == 60
        stored = Breakdown.from_json(score.breakdown or {})
        assert stored == replace(stored, caps=((60, "salary unlisted"),))
