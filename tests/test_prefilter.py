"""The pre-filter rules on their own, with the real filters.yaml and synthetic jobs."""

from typing import Any

import pytest

from job_scout.config import AppConfig
from job_scout.db.models import Job, SalaryPeriod
from job_scout.prefilter.rules import Prefilter


def FakeJob(**fields: Any) -> Job:
    """An unsaved Job with only the fields the rules read."""
    defaults: dict[str, Any] = {
        "id": 1,
        "source_id": "board",
        "title": "Senior Backend Engineer",
        "location_raw": None,
        "remote_raw": None,
        "description_text": None,
        "salary_min": None,
        "salary_max": None,
        "salary_currency": None,
        "salary_period": None,
    }
    return Job(**{**defaults, **fields})


@pytest.fixture
def prefilter(app_config: AppConfig) -> Prefilter:
    return Prefilter(app_config.filters, {"hn": "remote_mention_only"})


@pytest.mark.parametrize(
    ("job", "reason_start"),
    [
        (FakeJob(title="Account Executive"), "title: matches no include pattern"),
        (FakeJob(title="Junior Backend Developer"), "title: excluded by"),
        (FakeJob(title="Backend Solutions Engineer"), "title: excluded by"),
        (FakeJob(location_raw="Austin, Texas (hybrid)"), "remote: '\\\\bhybrid\\\\b' matched"),
        (FakeJob(remote_raw="on-site (Office)"), "remote: '\\\\bon[- ]?site\\\\b' matched"),
        (FakeJob(title="Backend Engineer (Onsite)"), "remote:"),
        # Location drops only when it's explicit that the applicant must be there.
        (FakeJob(location_raw="region: USA Only"), "location:"),
        (FakeJob(location_raw="usa only"), "location:"),
        (FakeJob(location_raw="Remote (US-only)"), "location:"),
        (FakeJob(location_raw="US/Canada only"), "location:"),
        (FakeJob(location_raw="EMEA only"), "location:"),
        (FakeJob(location_raw="United States residents only"), "location:"),
        (FakeJob(title="Backend Engineer (EU Applicants Only)"), "location:"),
        (FakeJob(location_raw="Remote; must reside in the U.S."), "location:"),
        (FakeJob(location_raw="Remote, U.S. citizenship required"), "location:"),
        (FakeJob(title="Backend Engineer (Security Clearance)"), "location:"),
        (FakeJob(salary_max=50, salary_currency="USD", salary_period=SalaryPeriod.HOUR), "salary:"),
        (
            FakeJob(salary_max=9_000, salary_currency="USD", salary_period=SalaryPeriod.MONTH),
            "salary:",
        ),
        (
            FakeJob(salary_max=110_000, salary_currency="USD", salary_period=SalaryPeriod.YEAR),
            "salary:",
        ),
    ],
)
def test_drops(prefilter: Prefilter, job: Job, reason_start: str) -> None:
    verdict = prefilter.evaluate(job)
    assert not verdict.passed
    assert verdict.reason.startswith(reason_start), verdict.reason


def test_drop_reason_names_the_matched_text(prefilter: Prefilter) -> None:
    verdict = prefilter.evaluate(
        FakeJob(salary_max=50, salary_currency="USD", salary_period=SalaryPeriod.HOUR)
    )
    assert verdict.reason == "salary: top of range 104,000 USD/yr < 120,000 USD/yr"
    verdict = prefilter.evaluate(FakeJob(location_raw="region: USA Only"))
    assert "'USA Only' in location" in verdict.reason


@pytest.mark.parametrize(
    ("job", "note"),
    [
        (
            FakeJob(location_raw="Anywhere in the World"),
            "location allowed ('Anywhere' in location)",
        ),
        # An allow match wins over an exclude match: contradictions go to Claude.
        (FakeJob(location_raw="Worldwide (US-only payroll)"), "location allowed"),
        # A bare place is unclear: Claude reads the description (profile.md).
        (FakeJob(location_raw="United States"), "location unclear, left to Claude"),
        (FakeJob(location_raw="usa"), "location unclear, left to Claude"),
        (FakeJob(location_raw="Remote - US"), "location unclear, left to Claude"),
        (FakeJob(location_raw="Canada (remote)"), "location unclear, left to Claude"),
        (FakeJob(location_raw="EMEA"), "location unclear, left to Claude"),
        (FakeJob(location_raw="US-based"), "location unclear, left to Claude"),
        (FakeJob(location_raw="region: North America Only"), "location unclear, left to Claude"),
        (FakeJob(location_raw="Remote, no US work authorization required"), "location unclear"),
        (FakeJob(title="Backend Engineer, join us only if you love Python"), "location unclear"),
        (FakeJob(location_raw="Guatemala,México"), "location allowed ('Guatemala' in location)"),
        (
            FakeJob(salary_max=11_000, salary_currency="USD", salary_period=SalaryPeriod.MONTH),
            "salary meets floor (132,000 USD/yr)",
        ),
        (
            FakeJob(salary_max=90_000, salary_currency="EUR", salary_period=SalaryPeriod.YEAR),
            "salary not comparable",
        ),
        (
            FakeJob(salary_max=150_000, salary_currency="USD", salary_period=None),
            "salary not comparable",
        ),
        (FakeJob(), "salary unlisted"),
    ],
)
def test_passes(prefilter: Prefilter, job: Job, note: str) -> None:
    verdict = prefilter.evaluate(job)
    assert verdict.passed, verdict.reason
    assert note in verdict.reason


def test_descriptions_are_not_regex_matched(prefilter: Prefilter) -> None:
    """'hybrid cloud', 'US-based benefits' etc. in a description must not drop a job."""
    job = FakeJob(
        description_text="Hybrid cloud platform. Benefits for US-based staff. In-office snacks."
    )
    assert prefilter.evaluate(job).passed


def test_programmer_titles_pass_for_game_studios(prefilter: Prefilter) -> None:
    assert prefilter.evaluate(FakeJob(title="Senior Online Services Programmer")).passed


@pytest.mark.parametrize(
    "title",
    [
        "Forward Deployed Engineer",
        "Staff Engineer",
        "Senior Integration Engineer",
        "Principal Engineer",
        "Senior Engineer (.NET)",
    ],
)
def test_ambiguous_engineering_titles_go_to_claude(prefilter: Prefilter, title: str) -> None:
    assert prefilter.evaluate(FakeJob(title=title)).passed


@pytest.mark.parametrize(
    "title", ["Senior Solutions Engineer", "ASIC Physical Design Engineer", "Engineering Manager"]
)
def test_clear_non_fits_still_dropped(prefilter: Prefilter, title: str) -> None:
    assert not prefilter.evaluate(FakeJob(title=title)).passed


def test_remote_mention_only_mode(prefilter: Prefilter) -> None:
    hn = FakeJob(source_id="hn", title="Acme | Staff PM | Full-time")
    assert not prefilter.evaluate(hn).passed
    assert prefilter.evaluate(
        FakeJob(source_id="hn", title=hn.title, description_text="We are fully REMOTE.")
    ).passed
    # Only the remote mention matters: titles and locations are left to Claude.
    assert prefilter.evaluate(
        FakeJob(source_id="hn", title="Acme | Designer | REMOTE (US only)")
    ).passed


def test_group_passes_via_any_copy(prefilter: Prefilter) -> None:
    emea = FakeJob(id=1, location_raw="EMEA only", source_id="4dayweek")
    latam = FakeJob(id=2, location_raw="LATAM", source_id="4dayweek")
    verdict = prefilter.evaluate_group([emea, latam])
    assert verdict.passed
    assert verdict.reason.endswith("(via copy #2 on 4dayweek) [2 copies]")


def test_group_drops_with_first_copy_reason(prefilter: Prefilter) -> None:
    verdict = prefilter.evaluate_group(
        [FakeJob(id=1, location_raw="EMEA only"), FakeJob(id=2, location_raw="APAC only")]
    )
    assert not verdict.passed
    assert verdict.reason.startswith("location:") and "'EMEA only'" in verdict.reason
    assert verdict.reason.endswith("[2 copies]")


def test_fingerprint_tracks_rules_and_modes(app_config: AppConfig) -> None:
    base = Prefilter(app_config.filters, {"hn": "remote_mention_only"})
    assert (
        base.fingerprint == Prefilter(app_config.filters, {"hn": "remote_mention_only"}).fingerprint
    )
    assert base.fingerprint != Prefilter(app_config.filters, {}).fingerprint
    stricter = app_config.filters.model_copy(
        update={"salary": app_config.filters.salary.model_copy(update={"min_annual": 130_000})}
    )
    assert base.fingerprint != Prefilter(stricter, {"hn": "remote_mention_only"}).fingerprint
