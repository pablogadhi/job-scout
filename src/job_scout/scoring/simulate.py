"""What a points table would do to the stored scores (`job-scout simulate`). Read-only: it
computes every stored result under a candidate table and compares it with the stored
score, so weights can be tuned without calling Claude or changing the database."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from statistics import mean

from sqlalchemy.orm import Session

from job_scout.config.points import PointsTable
from job_scout.config.settings import Digest as DigestSettings
from job_scout.report.data import ReportItem, build_sections, scored_items
from job_scout.scoring.points import Breakdown, compute

BANDS = ((85, "85-100"), (70, "70-84"), (50, "50-69"), (21, "21-49"), (0, "0-20"))
# Jobs alike on these are compared: any spread left among them has a cause in the breakdown.
# Disqualified jobs are left out: their cause (a rule on any field) is already plain.
GROUP_FIELDS = ("seniority_level", "role_category", "salary", "location_eligibility")


def band(score: int) -> str:
    return next(name for floor, name in BANDS if score >= floor)


@dataclass(frozen=True)
class Mover:
    job_id: int
    title: str
    company: str
    before: int
    after: int
    line: str


@dataclass(frozen=True)
class Spread:
    alike: tuple[str, ...]  # the GROUP_FIELDS values
    jobs: int
    lowest: int
    highest: int


@dataclass
class Simulation:
    compared: int = 0
    legacy: int = 0  # scored before points: no facts to compute from, left out
    bands_before: Counter[str] = field(default_factory=Counter)
    bands_after: Counter[str] = field(default_factory=Counter)
    sections_before: Counter[str] = field(default_factory=Counter)
    sections_after: Counter[str] = field(default_factory=Counter)
    component_means: dict[str, float] = field(default_factory=dict)
    movers: list[Mover] = field(default_factory=list)
    spreads: list[Spread] = field(default_factory=list)


def _sections(items: Sequence[ReportItem], settings: DigestSettings) -> Counter[str]:
    sections = build_sections(items, settings, include_below=True)
    return Counter(
        watchlist=len(sections.watchlist),
        top=len(sections.top),
        unclear=len(sections.unclear),
        below=len(sections.below),
    )


def simulate(
    session: Session,
    table: PointsTable,
    settings: DigestSettings,
    watchlist: set[str],
    *,
    movers: int = 15,
) -> Simulation:
    """Every scored job with facts to compute from, stored score vs `table`'s."""
    sim = Simulation()
    pairs: list[tuple[ReportItem, Breakdown]] = []
    for item in scored_items(session, watchlist):
        breakdown = compute(table, item.score.result or {})
        if breakdown is None:
            sim.legacy += 1
        else:
            pairs.append((item, breakdown))
    sim.compared = len(pairs)
    if not pairs:
        return sim
    before = [item for item, _ in pairs]
    stored = {item.job_id: item.overall for item in before}
    sim.sections_before = _sections(before, settings)

    session.expunge_all()  # the scores are changed in memory below, never saved
    after = []
    for item, breakdown in pairs:
        item.score.overall_score = breakdown.total
        item.score.disqualified = breakdown.disqualified
        item.score.breakdown = breakdown.to_json()
        after.append(replace(item))  # a fresh item: no cached breakdown
    sim.sections_after = _sections(after, settings)

    for item, breakdown in pairs:
        sim.bands_before[band(stored[item.job_id])] += 1
        sim.bands_after[band(breakdown.total)] += 1
    parts: dict[str, list[int]] = defaultdict(list)
    for _, breakdown in pairs:
        for part in breakdown.parts:
            parts[part.label].append(part.points)
    sim.component_means = {label: round(mean(points), 1) for label, points in parts.items()}

    moved = sorted(pairs, key=lambda p: abs(p[1].total - stored[p[0].job_id]), reverse=True)
    sim.movers = [
        Mover(item.job_id, item.title, item.company, stored[item.job_id], b.total, b.line())
        for item, b in moved[:movers]
        if b.total != stored[item.job_id]
    ]

    alike: dict[tuple[str, ...], list[int]] = defaultdict(list)
    for item, breakdown in pairs:
        if breakdown.disqualified:
            continue
        result = item.score.result or {}
        alike[tuple(str(result.get(name)) for name in GROUP_FIELDS)].append(breakdown.total)
    spreads = [Spread(k, len(v), min(v), max(v)) for k, v in alike.items() if len(v) > 1]
    sim.spreads = sorted(spreads, key=lambda s: (s.highest - s.lowest, s.jobs), reverse=True)[:10]
    return sim
