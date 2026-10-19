"""Claude's structured output: the fields in profile.md's "Output format" plus `job_id`.

One schema for both backends and any batch size: `{"results": [ScoreResult, ...]}`. The
models are built per profile (`score_schema`), in the profile's field order. The core
fields keep `CoreResult`'s types, which the code relies on, and the profile must declare
them the same way; every other field is typed from its example (config/profile.py).
"""

from __future__ import annotations

import copy
import dataclasses
import types
from dataclasses import dataclass
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, ConfigDict, create_model

from job_scout.config.profile import OutputField, Profile


class CoreResult(BaseModel):
    """A result as the code reads it: the core fields, typed. The profile's other fields stay
    as they are (`model_extra`), so a stored result reads under any profile, including one
    scored before points (it also carries Claude's overall_score and disqualified)."""

    model_config = ConfigDict(extra="allow")

    remote: Literal["yes", "no", "unclear"]
    location_eligibility: Literal["pass", "fail", "unclear"]
    salary: Literal["meets", "near", "below", "unlisted"]
    red_flags: list[str]
    summary: str


# Core enum values a profile may leave out: `near` (between a soft and a hard salary minimum)
# only exists for a profile with two minimums.
OPTIONAL_VALUES: dict[str, frozenset[str]] = {"salary": frozenset({"near"})}


class SchemaMismatch(ValueError):
    pass


_SCALARS: dict[Any, Literal["bool", "int", "float", "str"]] = {
    bool: "bool",
    int: "int",
    float: "float",
    str: "str",
}


def _spec(name: str, annotation: Any) -> OutputField:
    """The OutputField a profile must declare for a core field typed `annotation`."""
    nullable = False
    if get_origin(annotation) in (Union, types.UnionType) and type(None) in get_args(annotation):
        nullable = True
        annotation = next(a for a in get_args(annotation) if a is not type(None))
    if get_origin(annotation) is Literal:
        return OutputField(name, "enum", tuple(get_args(annotation)), nullable)
    if get_origin(annotation) is list:
        return OutputField(name, "list", nullable=nullable)
    return OutputField(name, _SCALARS[annotation], nullable=nullable)


def _declared_as(field: OutputField, expected: OutputField) -> bool:
    """`field` is `expected`, or `expected` without some of its optional values."""
    optional = OPTIONAL_VALUES.get(field.name, frozenset())
    kept = tuple(v for v in expected.values if v not in optional or v in field.values)
    return field == dataclasses.replace(expected, values=kept)


def _annotation(field: OutputField) -> Any:
    base: Any
    if field.kind == "enum":
        base = Literal[field.values]  # pyright: ignore[reportInvalidTypeForm]
    elif field.kind == "list":
        base = list[str]
    elif field.kind == "objects":
        fields: dict[str, Any] = {f.name: (_annotation(f), ...) for f in field.fields}
        item = create_model(
            _model_name(field.name), __config__=ConfigDict(extra="forbid"), **fields
        )
        base = list[item]  # pyright: ignore[reportInvalidTypeForm]
    else:
        base = {"bool": bool, "int": int, "float": float, "str": str}[field.kind]
    return base | None if field.nullable else base


def _model_name(field_name: str) -> str:
    """`requirements` -> `RequirementsItem`: the item's name in the JSON schema's $defs."""
    return "".join(part.capitalize() for part in field_name.split("_")) + "Item"


# Keywords structured outputs rejects; Pydantic still enforces them when parsing the reply.
_UNSUPPORTED = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
                "minLength", "maxLength", "title", "default"}  # fmt: skip


def _strip(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            # Keys under "properties"/"$defs" are names, not keywords: never drop them.
            k: {name: _strip(sub) for name, sub in v.items()}
            if k in ("properties", "$defs")
            else _strip(v)
            for k, v in node.items()
            if k not in _UNSUPPORTED
        }
    if isinstance(node, list):
        return [_strip(v) for v in node]
    return node


@dataclass(frozen=True)
class ScoreSchema:
    """The models for one profile: a reply item (`result`) and the whole reply (`batch`)."""

    result: type[BaseModel]
    batch: type[BaseModel]

    def json_schema(self) -> dict[str, Any]:
        """JSON schema for `{"results": [...]}` that both the API and the CLI accept."""
        return _strip(copy.deepcopy(self.batch.model_json_schema()))

    def parse(self, item: Any) -> tuple[dict[str, Any], CoreResult]:
        """Validate one reply item. Returns it as stored (the profile's field order, with
        job_id) and its typed core. Raises pydantic's ValidationError."""
        data = self.result.model_validate(item).model_dump()
        return data, CoreResult.model_validate(data)


_SCHEMAS: dict[str, ScoreSchema] = {}


def score_schema(profile: Profile) -> ScoreSchema:
    """The models for `profile`. Raises SchemaMismatch if a core field is declared differently
    from `CoreResult` (e.g. another set of values for `remote`)."""
    if cached := _SCHEMAS.get(profile.sha256):
        return cached
    core = CoreResult.model_fields
    mismatched = [
        f"{f.name} (expected {expected})"
        for f in profile.output_fields
        if f.name in core
        and not _declared_as(f, expected := _spec(f.name, core[f.name].annotation))
    ]
    if mismatched:
        raise SchemaMismatch(
            "profile.md output format declares core fields differently from the code: "
            + "; ".join(mismatched)
        )
    fields: dict[str, Any] = {"job_id": (int, ...)}
    for f in profile.output_fields:
        if f.name in core:
            # The profile's own values: a value it left out is one Claude can't give.
            annotation = _annotation(f) if f.kind == "enum" else core[f.name].annotation
            fields[f.name] = (annotation, copy.copy(core[f.name]))
        else:
            fields[f.name] = (_annotation(f), ...)
    forbid = ConfigDict(extra="forbid")
    result = create_model("ScoreResult", __config__=forbid, **fields)
    batch = create_model(
        "ScoreBatch",
        __config__=forbid,
        results=(list[result], ...),  # pyright: ignore[reportInvalidTypeForm]
    )
    schema = _SCHEMAS[profile.sha256] = ScoreSchema(result, batch)
    return schema
