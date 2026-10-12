"""Code-side pre-filter (config/filters.yaml): drop only clear non-fits before scoring.

Rules run in order (title, remote, location, salary); the first failing rule decides and is
recorded as the reason. Anything unclear passes through to Claude.

Which text each rule reads (descriptions are never regex-matched; see DECISIONS.md P2-D2):
- title rules:     title
- remote rules:    title, location, the source's remote/workplace field
- location rules:  title, location (the `^remote - us$`-style pattern needs the whole field)
- salary:          the parsed salary fields, annualized; only in filters.salary.currency

A duplicate group (a job and its linked copies on other boards or for other countries)
passes if ANY copy passes, so an eligible copy is never lost behind an ineligible first one.
Sources with `prefilter: remote_mention_only` (HN comments: free text, no real title) only
need to mention "remote" somewhere.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from job_scout.config.filters import FiltersConfig

# Bump when the rule logic changes, so stored results are re-evaluated.
PREFILTER_VERSION = 1

PrefilterMode = Literal["full", "remote_mention_only"]

_REMOTE_MENTION = re.compile(r"\bremote\b", re.IGNORECASE)


class JobLike(Protocol):
    id: int
    source_id: str
    title: str
    location_raw: str | None
    remote_raw: str | None
    description_text: str | None
    salary_min: float | None
    salary_max: float | None
    salary_currency: str | None
    salary_period: str | None


@dataclass(frozen=True)
class Verdict:
    passed: bool
    reason: str


@dataclass(frozen=True)
class _Rule:
    pattern: re.Pattern[str]

    def search(self, fields: Mapping[str, str | None]) -> tuple[str, str] | None:
        """(field name, matched text) for the first field this rule matches."""
        for name, value in fields.items():
            if value and (match := self.pattern.search(value)):
                return name, match.group(0)
        return None


def _compile(patterns: Sequence[str]) -> list[_Rule]:
    return [_Rule(re.compile(p, re.IGNORECASE)) for p in patterns]


def _first_match(
    rules: Sequence[_Rule], fields: Mapping[str, str | None]
) -> tuple[_Rule, str, str] | None:
    for rule in rules:
        if hit := rule.search(fields):
            return rule, *hit
    return None


def _period_value(period: str) -> str:
    # The ORM hands back a SalaryPeriod enum; Postings carry plain strings.
    return getattr(period, "value", period)


class Prefilter:
    def __init__(self, filters: FiltersConfig, modes: Mapping[str, PrefilterMode]) -> None:
        self.filters = filters
        self.modes = modes
        self.title_include = _compile(filters.titles.include_any)
        self.title_exclude = _compile(filters.titles.exclude_any)
        self.remote_exclude = _compile(filters.remote.exclude_any)
        self.location_allow = _compile(filters.location.allow_any)
        self.location_exclude = _compile(filters.location.exclude_any)
        salary = filters.salary
        self.per_year = {
            "hour": salary.hours_per_year,
            "day": salary.hours_per_year / 8,
            "week": salary.hours_per_year / 40,
            "month": salary.months_per_year,
            "year": 1,
        }

    @property
    def fingerprint(self) -> str:
        """Changes whenever filters.yaml, a source's prefilter mode or the rule code changes."""
        payload = "|".join(
            [
                str(PREFILTER_VERSION),
                self.filters.model_dump_json(),
                ",".join(f"{k}={v}" for k, v in sorted(self.modes.items())),
            ]
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def annual_salary(self, job: JobLike) -> float | None:
        """Top of the stated range per year, in filters.salary.currency; None if unknown."""
        top = job.salary_max or job.salary_min
        if not top or not job.salary_period or job.salary_currency != self.filters.salary.currency:
            return None
        return top * self.per_year[_period_value(job.salary_period)]

    def evaluate(self, job: JobLike) -> Verdict:
        if self.modes.get(job.source_id, "full") == "remote_mention_only":
            text = " ".join(filter(None, [job.title, job.description_text]))
            if _REMOTE_MENTION.search(text):
                return Verdict(True, "free-text post mentions remote; the rest is left to Claude")
            return Verdict(False, "free-text post: no mention of remote")

        title = {"title": job.title}
        if not _first_match(self.title_include, title):
            return Verdict(False, "title: matches no include pattern")
        if hit := _first_match(self.title_exclude, title):
            rule, _, text = hit
            return Verdict(False, f"title: excluded by {rule.pattern.pattern!r} ({text!r})")

        remote_fields = {"title": job.title, "location": job.location_raw, "remote": job.remote_raw}
        if hit := _first_match(self.remote_exclude, remote_fields):
            rule, field, text = hit
            return Verdict(False, f"remote: {rule.pattern.pattern!r} matched {text!r} in {field}")

        notes: list[str] = []
        location_fields = {"title": job.title, "location": job.location_raw}
        if hit := _first_match(self.location_allow, location_fields):
            notes.append(f"location allowed ({hit[2]!r} in {hit[1]})")
        elif hit := _first_match(self.location_exclude, location_fields):
            rule, field, text = hit
            return Verdict(False, f"location: {rule.pattern.pattern!r} matched {text!r} in {field}")
        else:
            notes.append("location unclear, left to Claude")

        annual = self.annual_salary(job)
        floor = self.filters.salary.min_annual
        currency = self.filters.salary.currency
        if annual is not None and annual < floor:
            return Verdict(
                False, f"salary: top of range {annual:,.0f} {currency}/yr < {floor:,} {currency}/yr"
            )
        if annual is not None:
            notes.append(f"salary meets floor ({annual:,.0f} {currency}/yr)")
        elif job.salary_max or job.salary_min:
            notes.append("salary not comparable (currency or period), left to Claude")
        else:
            notes.append("salary unlisted")
        return Verdict(True, "; ".join(notes))

    def evaluate_group(self, jobs: Sequence[JobLike]) -> Verdict:
        """Pass if any copy passes. jobs[0] is the canonical job."""
        verdicts = [(job, self.evaluate(job)) for job in jobs]
        size = f" [{len(jobs)} copies]" if len(jobs) > 1 else ""
        for job, verdict in verdicts:
            if verdict.passed:
                via = f" (via copy #{job.id} on {job.source_id})" if job is not jobs[0] else ""
                return Verdict(True, f"{verdict.reason}{via}{size}")
        return Verdict(False, f"{verdicts[0][1].reason}{size}")
