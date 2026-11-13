"""config/scoring.yaml: points for the facts Claude extracts (profile.md's output format).

The code turns each stored result into a score with this table; Claude never sees it, so a
weight can't steer what Claude reports, and changing one re-ranks stored scores for free.
Every component reads one output field:
- `enum`: points per value of an enum field (every value listed, nothing else)
- `requirements`: a list of objects (e.g. skills), `max` times the weighted share matched
- `signals`: a list of objects naming a signal (e.g. a 4-day week), points per signal,
  summed and clamped
- `number`: a number field (e.g. the years of experience asked), points (and an optional
  cap) by the band the value falls in; `relative_to: profile_years` writes the bands as
  years above the profile's own (`over_by`), resolved when the config loads
`caps` and `disqualify` rules match enum fields. `scoring.points.check_points` checks the
table against the profile at startup.
"""

from __future__ import annotations

from itertools import pairwise
from typing import Annotated, Literal

from pydantic import Field, PositiveInt, model_validator

from job_scout.config.settings import StrictModel


class EnumComponent(StrictModel):
    kind: Literal["enum"]
    field: str
    label: str | None = None  # in the breakdown; the component's key when unset
    points: dict[str, int]


class SubfieldValues(StrictModel):
    """A number for each value of one enum field of the listed objects."""

    subfield: str
    values: dict[str, float]


class PrimaryMissing(StrictModel):
    """When an item flagged by `subfield` (true/false) has a match in `when_match`: the
    component is worth at most `component_max`, the whole score at most `total_max`."""

    subfield: str
    when_match: list[str] = Field(min_length=1)
    component_max: int = Field(ge=0)
    total_max: int = Field(ge=0, le=100)
    reason: str


class RequirementsComponent(StrictModel):
    kind: Literal["requirements"]
    field: str
    label: str | None = None
    item: str  # the subfield that names an item in the breakdown (e.g. skill)
    evidence: str | None = None  # the subfield holding the posting's quote
    max: PositiveInt
    weight: SubfieldValues
    match: SubfieldValues
    if_empty: float = Field(default=0.5, ge=0, le=1)  # share matched when nothing is listed
    primary_missing: PrimaryMissing | None = None

    @model_validator(mode="after")
    def _sane_numbers(self) -> RequirementsComponent:
        if any(w <= 0 for w in self.weight.values.values()):
            raise ValueError("weight values must be above 0")
        if any(not 0 <= m <= 1 for m in self.match.values.values()):
            raise ValueError("match values must be between 0 and 1")
        return self


class Signal(StrictModel):
    points: int
    label: str | None = None  # in the breakdown; the signal's name when unset
    badge: bool = False  # shown next to the job's title when present


class SignalsComponent(StrictModel):
    kind: Literal["signals"]
    field: str
    label: str | None = None
    subfield: str  # the enum subfield naming the signal
    evidence: str | None = None
    min: int = -20
    max: int = 20
    points: dict[str, int | Signal]

    @model_validator(mode="after")
    def _range(self) -> SignalsComponent:
        if self.min > self.max:
            raise ValueError("min must not be above max")
        return self

    def signal(self, name: str) -> Signal | None:
        spec = self.points.get(name)
        return Signal(points=spec) if isinstance(spec, int) else spec


class Band(StrictModel):
    """Values up to and including `upto`, or `over_by` above the profile's years (the last
    band has neither: everything above). `max` caps the whole score, giving `reason`."""

    upto: float | None = None
    over_by: float | None = None
    points: int
    max: int | None = Field(default=None, ge=0, le=100)
    reason: str | None = None

    @model_validator(mode="after")
    def _cap_has_reason(self) -> Band:
        if (self.max is None) != (self.reason is None):
            raise ValueError("a band's max and reason go together")
        return self


class NumberComponent(StrictModel):
    kind: Literal["number"]
    field: str
    label: str | None = None
    unit: str | None = None  # after the value in the breakdown ("8 years")
    # The bands count from the years in profile.md's Summary (`over_by`) instead of from 0
    # (`upto`), so the CV stays the one place that says how experienced the candidate is.
    relative_to: Literal["profile_years"] | None = None
    bands: list[Band] = Field(min_length=1)  # the first one whose bound covers the value

    @model_validator(mode="after")
    def _ordered(self) -> NumberComponent:
        name, other = ("over_by", "upto") if self.relative_to else ("upto", "over_by")
        *bounded, last = self.bands
        if any(getattr(b, other) is not None for b in self.bands):
            raise ValueError(f"bands take {name}, not {other}, here")
        bounds = [getattr(b, name) for b in bounded]
        if None in bounds or getattr(last, name) is not None:
            raise ValueError(f"every band but the last needs {name}; the last has none")
        if any(a >= b for a, b in pairwise(bounds)):
            raise ValueError(f"band {name} values must increase")
        return self

    def resolved(self, profile_years: int | None) -> NumberComponent:
        """The component with absolute bounds (`upto`); unchanged when it already has them."""
        if self.relative_to is None:
            return self
        if profile_years is None:
            raise ValueError(
                f"{self.field}: relative_to profile_years, but profile.md's Summary states no "
                "years of experience (e.g. '6+ years of experience')"
            )
        bands = [
            b.model_copy(
                update={
                    "upto": None if b.over_by is None else profile_years + b.over_by,
                    "over_by": None,
                }
            )
            for b in self.bands
        ]
        return self.model_copy(update={"relative_to": None, "bands": bands})

    def band(self, value: float) -> Band:
        return next(b for b in self.bands if b.upto is None or value <= b.upto)


Component = Annotated[
    EnumComponent | RequirementsComponent | SignalsComponent | NumberComponent,
    Field(discriminator="kind"),
]


class Rule(StrictModel):
    """Matches when every named enum field has one of its listed values."""

    when: dict[str, list[str]] = Field(min_length=1)
    reason: str


class Cap(Rule):
    max: int = Field(ge=0, le=100)


class PointsTable(StrictModel):
    components: dict[str, Component] = Field(min_length=1)  # breakdown order
    caps: list[Cap] = []
    disqualify: list[Rule] = []
    disqualified_max: int = Field(default=20, ge=0, le=100)

    def resolved(self, profile_years: int | None) -> PointsTable:
        """The table with every relative band made absolute from the profile's years, as
        the code scores with it (its fingerprint changes with those years). Raises
        ValueError when a component needs years the profile doesn't state."""
        components = {
            key: c.resolved(profile_years) if isinstance(c, NumberComponent) else c
            for key, c in self.components.items()
        }
        return self.model_copy(update={"components": components})
