"""Candidate profile (config/profile.md), used verbatim as the scoring system prompt.

The "## Output format" JSON block defines the fields Claude returns, by example. Each
field's type comes from its example value:
- `"a | b | c"`: one of those values
- `"text"`: a string; `null`, or a string ending in "or null", allows null
- `["text"]`: a list of strings
- `[{"name": "text", "level": "a | b"}]`: a list of objects with those fields, each typed
  like a top-level field but never a list, an object or null
- `true`/`false`, a whole number, a decimal: a boolean, an integer, a number
The core fields (`CORE_OUTPUT_KEYS`) must be there; the code relies on them. Every other
field is the profile's own and is shown as it is.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

# Fields the code relies on (sections, ranking, location and salary checks, reports).
CORE_OUTPUT_KEYS = frozenset(
    {
        "disqualified",
        "disqualify_reason",
        "remote",
        "location_eligibility",
        "salary",
        "matched_skills",
        "gaps",
        "red_flags",
        "overall_score",
        "summary",
    }
)

_OUTPUT_BLOCK = re.compile(r"^## Output format\b.*?^```json\n(.*?)^```", re.DOTALL | re.MULTILINE)
_OR_NULL = re.compile(r"\bor null\s*$")

FieldKind = Literal["bool", "int", "float", "str", "enum", "list", "objects"]


class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class OutputField:
    """One field of the output format, typed from its example value."""

    name: str
    kind: FieldKind
    values: tuple[str, ...] = ()  # kind "enum": the allowed values, in the profile's order
    nullable: bool = False
    fields: tuple[OutputField, ...] = ()  # kind "objects": each object's fields, in order


@dataclass(frozen=True)
class Profile:
    path: Path
    text: str
    sha256: str
    output_example: dict[str, Any]
    output_fields: tuple[OutputField, ...]


def output_field(name: str, example: Any) -> OutputField:
    if isinstance(example, bool):
        return OutputField(name, "bool")
    if isinstance(example, int):
        return OutputField(name, "int")
    if isinstance(example, float):
        return OutputField(name, "float")
    if example is None:
        return OutputField(name, "str", nullable=True)
    if isinstance(example, str):
        if " | " in example:
            values = tuple(v.strip() for v in example.split("|"))
            if not all(values) or len(set(values)) != len(values):
                raise ProfileError(f"{name}: empty or repeated value in {example!r}")
            return OutputField(name, "enum", values)
        return OutputField(name, "str", nullable=bool(_OR_NULL.search(example)))
    if isinstance(example, list) and all(isinstance(v, str) for v in example):
        return OutputField(name, "list")
    if isinstance(example, list) and len(example) == 1 and isinstance(example[0], dict):
        if not example[0]:
            raise ProfileError(f"{name}: the example object has no fields")
        return OutputField(
            name, "objects", fields=tuple(_object_field(name, k, v) for k, v in example[0].items())
        )
    raise ProfileError(
        f"{name}: unsupported example {example!r} (use text, 'a | b', a list of text, "
        "a list holding one example object, true/false, a number or null)"
    )


def _object_field(parent: str, name: str, example: Any) -> OutputField:
    # Flat and never null: each nullable field is a union, and the API limits those.
    field = output_field(f"{parent}.{name}", example)
    if field.kind in ("list", "objects") or field.nullable:
        raise ProfileError(
            f"{parent}.{name}: fields of a list of objects are text, 'a | b', true/false "
            "or a number, never a list, an object or null"
        )
    return replace(field, name=name)


def load_profile(path: Path) -> Profile:
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    match = _OUTPUT_BLOCK.search(text)
    if not match:
        raise ProfileError("no ```json block under a '## Output format' heading")
    try:
        example = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ProfileError(f"output format block is not valid JSON: {exc}") from exc
    if not isinstance(example, dict):
        raise ProfileError("output format block must be a JSON object")
    missing = CORE_OUTPUT_KEYS - example.keys()
    if missing:
        raise ProfileError(f"output format block is missing keys: {', '.join(sorted(missing))}")
    if "job_id" in example:
        raise ProfileError("output format block must not define job_id (the code adds it)")
    return Profile(
        path=path,
        text=text,
        sha256=hashlib.sha256(raw).hexdigest(),
        output_example=example,
        output_fields=tuple(output_field(name, value) for name, value in example.items()),
    )
