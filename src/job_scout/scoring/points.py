"""The score, computed by code from the facts Claude extracted and config/scoring.yaml.

`compute` is pure: the same stored result and table always give the same score and
breakdown. A result without a field the table reads (one scored before this format) has no
breakdown; it keeps Claude's own number until it's scored again.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from job_scout.config.points import (
    EnumComponent,
    PointsTable,
    RequirementsComponent,
    Rule,
    SignalsComponent,
)
from job_scout.config.profile import OutputField, Profile
from job_scout.db.models import Score, ScoreStatus

# Bump whenever the formula itself changes, so stored scores are recomputed.
SCORING_VERSION = 1


class PointsMismatch(ValueError):
    pass


def fingerprint(table: PointsTable) -> str:
    """Changes with any value in scoring.yaml or the formula, not with comments or layout."""
    payload = f"{SCORING_VERSION}|{table.model_dump_json()}"
    return hashlib.sha256(payload.encode()).hexdigest()


# --- the table against the profile -----------------------------------------------------


def check_points(table: PointsTable, profile: Profile) -> None:
    """Every field, subfield and value the table names must exist in the profile's output
    format, and every enum map must list all of its field's values."""
    fields = {f.name: f for f in profile.output_fields}
    problems: list[str] = []

    def enum_field(where: str, name: str, pool: Mapping[str, OutputField]) -> OutputField | None:
        f = pool.get(name)
        if f is None:
            problems.append(f"{where}: no field {name!r} in the output format")
        elif f.kind != "enum":
            problems.append(f"{where}: {name!r} is not an 'a | b' field")
        else:
            return f
        return None

    def exhaustive(where: str, f: OutputField, keys: set[str]) -> None:
        if missing := [v for v in f.values if v not in keys]:
            problems.append(f"{where}: no points for {', '.join(missing)}")
        if unknown := sorted(keys - set(f.values)):
            problems.append(f"{where}: {f.name!r} has no value {', '.join(unknown)}")

    def objects_field(where: str, name: str) -> dict[str, OutputField] | None:
        f = fields.get(name)
        if f is None:
            problems.append(f"{where}: no field {name!r} in the output format")
        elif f.kind != "objects":
            problems.append(f"{where}: {name!r} is not a list of objects")
        else:
            return {sub.name: sub for sub in f.fields}
        return None

    def subfield(where: str, subs: Mapping[str, OutputField], name: str | None) -> None:
        if name is not None and name not in subs:
            problems.append(f"{where}: the objects have no field {name!r}")

    for key, component in table.components.items():
        where = f"components.{key}"
        if isinstance(component, EnumComponent):
            if f := enum_field(where, component.field, fields):
                exhaustive(where, f, set(component.points))
        elif isinstance(component, RequirementsComponent):
            if (subs := objects_field(where, component.field)) is None:
                continue
            subfield(where, subs, component.item)
            subfield(where, subs, component.evidence)
            for part in (component.weight, component.match):
                if f := enum_field(f"{where}.{part.subfield}", part.subfield, subs):
                    exhaustive(f"{where}.{part.subfield}", f, set(part.values))
            if p := component.primary_missing:
                flag = subs.get(p.subfield)
                if flag is None or flag.kind != "bool":
                    problems.append(f"{where}.primary_missing: {p.subfield!r} is not true/false")
                if unknown := sorted(set(p.when_match) - set(component.match.values)):
                    problems.append(f"{where}.primary_missing: no match {', '.join(unknown)}")
        else:
            if (subs := objects_field(where, component.field)) is None:
                continue
            subfield(where, subs, component.evidence)
            if f := enum_field(f"{where}.{component.subfield}", component.subfield, subs):
                exhaustive(where, f, set(component.points))

    rules: list[tuple[str, Rule]] = [(f"caps[{i}]", r) for i, r in enumerate(table.caps)]
    rules += [(f"disqualify[{i}]", r) for i, r in enumerate(table.disqualify)]
    for where, rule in rules:
        for name, values in rule.when.items():
            f = enum_field(where, name, fields)
            if f and (unknown := sorted(set(values) - set(f.values))):
                problems.append(f"{where}: {name!r} has no value {', '.join(unknown)}")

    if problems:
        raise PointsMismatch("scoring.yaml doesn't match profile.md: " + "; ".join(problems))


# --- the breakdown ---------------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    """One requirement or signal as it was counted."""

    text: str
    points: float
    evidence: str | None = None
    note: str | None = None  # e.g. "required, core"


@dataclass(frozen=True)
class Part:
    """One component's points. `max` is None for components that only add or subtract."""

    name: str
    label: str
    points: int
    max: int | None
    items: tuple[Item, ...] = ()


@dataclass(frozen=True)
class Breakdown:
    parts: tuple[Part, ...]
    subtotal: int  # the parts added up, before caps and disqualification
    caps: tuple[tuple[int, str], ...]  # (max, reason) of each cap that lowered the score
    disqualified: bool
    disqualify_reason: str | None
    total: int
    badges: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()  # values the table doesn't know (counted as 0)
    version: int = field(default=SCORING_VERSION)

    def line(self) -> str:
        """`skills 34/40 · seniority 20/20 · signals +7 (async-first +4) = 81 → 45 (…)`"""
        shown = []
        for part in self.parts:
            if part.max is not None and part.max > 0:
                text = f"{part.label} {part.points}/{part.max}"
            elif part.points or part.items:
                text = f"{part.label} {part.points:+d}"
            else:
                continue
            if part.max is None and part.items:
                text += " (" + ", ".join(f"{i.text} {i.points:+g}" for i in part.items) + ")"
            shown.append(text)
        line = " · ".join(shown) + f" = {self.subtotal}"
        for cap, reason in self.caps:
            line += f" → {cap} ({reason})"
        if self.disqualified:
            line += f" → {self.total} (disqualified: {self.disqualify_reason})"
        return line

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Breakdown:
        parts = tuple(
            Part(**{**p, "items": tuple(Item(**i) for i in p["items"])}) for p in data["parts"]
        )
        return cls(
            **{
                **data,
                "parts": parts,
                "caps": tuple((c[0], c[1]) for c in data["caps"]),
                "badges": tuple(data["badges"]),
                "notes": tuple(data["notes"]),
            }
        )


def _round(x: float) -> int:
    """Half up, the same on every platform (Python's round() rounds half to even)."""
    return math.floor(x + 0.5)


def _label(name: str) -> str:
    return name.replace("_", " ")


def compute(table: PointsTable, result: Mapping[str, Any]) -> Breakdown | None:
    """The score for one stored result, or None when it lacks a field a component reads. A
    cap or disqualify rule on a field the result lacks (one added to the profile after it
    was scored) doesn't apply."""
    if any(c.field not in result for c in table.components.values()):
        return None

    notes: list[str] = []
    parts: list[Part] = []
    caps: list[tuple[int, str]] = []
    badges: list[str] = []
    for key, component in table.components.items():
        label = component.label or key
        if isinstance(component, EnumComponent):
            value = result[component.field]
            if value not in component.points:
                notes.append(f"{component.field}: unknown value {value!r}")
            parts.append(
                Part(key, label, component.points.get(value, 0), max(component.points.values()))
            )
        elif isinstance(component, RequirementsComponent):
            part, cap = _requirements(key, label, component, result[component.field], notes)
            parts.append(part)
            if cap:
                caps.append(cap)
        else:
            part, part_badges = _signals(key, label, component, result[component.field], notes)
            parts.append(part)
            badges += part_badges

    subtotal = sum(p.points for p in parts)
    total = subtotal
    rule_caps = [(c.max, c.reason) for c in table.caps if _matches(c, result)]
    applied = []
    for cap, reason in [*caps, *rule_caps]:
        if cap < total:
            total = cap
            applied.append((cap, reason))
    reasons = [r.reason for r in table.disqualify if _matches(r, result)]
    if reasons:
        total = min(total, table.disqualified_max)
    return Breakdown(
        parts=tuple(parts),
        subtotal=subtotal,
        caps=tuple(applied),
        disqualified=bool(reasons),
        disqualify_reason="; ".join(reasons) or None,
        total=max(0, min(100, total)),
        badges=tuple(badges),
        notes=tuple(notes),
    )


def _matches(rule: Rule, result: Mapping[str, Any]) -> bool:
    return all(result.get(name) in values for name, values in rule.when.items())


def _requirements(
    key: str, label: str, c: RequirementsComponent, listed: Any, notes: list[str]
) -> tuple[Part, tuple[int, str] | None]:
    counted: list[tuple[dict[str, Any], float, float]] = []
    for entry in listed or []:
        weight = c.weight.values.get(entry.get(c.weight.subfield))
        match = c.match.values.get(entry.get(c.match.subfield))
        if weight is None or match is None:
            notes.append(f"{c.field}: skipped {entry.get(c.item)!r} (unknown weight or match)")
            continue
        counted.append((entry, weight, match))
    total_weight = sum(w for _, w, _ in counted)
    share = sum(w * m for _, w, m in counted) / total_weight if total_weight else c.if_empty
    points = _round(c.max * share)
    items = tuple(
        Item(
            text=str(entry.get(c.item, "")),
            points=round(c.max * w * m / total_weight, 1),
            evidence=entry.get(c.evidence) if c.evidence else None,
            note=_note(c, entry),
        )
        for entry, w, m in counted
    )
    cap = None
    p = c.primary_missing
    if p and any(
        entry.get(p.subfield) is True and entry.get(c.match.subfield) in p.when_match
        for entry, _, _ in counted
    ):
        points = min(points, p.component_max)
        cap = (p.total_max, p.reason)
    return Part(key, label, points, c.max, items), cap


def _note(c: RequirementsComponent, entry: Mapping[str, Any]) -> str:
    """`required, core` or `required, missing, primary`."""
    note = [str(entry.get(c.weight.subfield)), str(entry.get(c.match.subfield))]
    if c.primary_missing and entry.get(c.primary_missing.subfield) is True:
        note.append("primary")
    return ", ".join(note)


def _signals(
    key: str, label: str, c: SignalsComponent, listed: Any, notes: list[str]
) -> tuple[Part, list[str]]:
    seen: set[str] = set()
    items: list[Item] = []
    badges: list[str] = []
    for entry in listed or []:
        name = entry.get(c.subfield)
        if name in seen:
            continue
        seen.add(name)
        spec = c.signal(name)
        if spec is None:
            notes.append(f"{c.field}: unknown signal {name!r}")
            continue
        text = spec.label or _label(name)
        items.append(Item(text, spec.points, entry.get(c.evidence) if c.evidence else None))
        if spec.badge:
            badges.append(text)
    points = max(c.min, min(c.max, sum(int(i.points) for i in items)))
    return Part(key, label, points, None, tuple(items)), badges


# --- stored scores ---------------------------------------------------------------------


def apply(score: Score, breakdown: Breakdown, fp: str) -> None:
    score.overall_score = breakdown.total
    score.disqualified = breakdown.disqualified
    score.breakdown = breakdown.to_json()
    score.score_fingerprint = fp


def refresh(session: Session, table: PointsTable) -> tuple[int, int]:
    """Recompute every scored row computed with another table or formula. Returns
    (recomputed, without a breakdown): a result in the format before points keeps Claude's
    number, and is looked at again only when the table changes."""
    fp = fingerprint(table)
    stale = session.scalars(
        select(Score).where(
            Score.status == ScoreStatus.SCORED,
            or_(Score.score_fingerprint.is_(None), Score.score_fingerprint != fp),
        )
    )
    recomputed = legacy = 0
    for score in stale:
        breakdown = compute(table, score.result) if score.result is not None else None
        if breakdown is None:
            score.breakdown = None
            score.score_fingerprint = fp
            legacy += 1
        else:
            apply(score, breakdown, fp)
            recomputed += 1
    return recomputed, legacy
