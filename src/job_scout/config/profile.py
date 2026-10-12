"""Candidate profile (config/profile.md), used verbatim as the scoring system prompt."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Keys the "## Output format" JSON block must define; the score model is derived from it.
REQUIRED_OUTPUT_KEYS = frozenset(
    {
        "disqualified",
        "disqualify_reason",
        "remote",
        "location_eligibility",
        "salary",
        "role_category",
        "skill_fit",
        "seniority_fit",
        "matched_skills",
        "gaps",
        "four_day_week",
        "red_flags",
        "overall_score",
        "summary",
    }
)

_OUTPUT_BLOCK = re.compile(r"^## Output format\b.*?^```json\n(.*?)^```", re.DOTALL | re.MULTILINE)


class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class Profile:
    path: Path
    text: str
    sha256: str
    output_example: dict[str, Any]


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
    missing = REQUIRED_OUTPUT_KEYS - example.keys()
    if missing:
        raise ProfileError(f"output format block is missing keys: {', '.join(sorted(missing))}")
    return Profile(
        path=path, text=text, sha256=hashlib.sha256(raw).hexdigest(), output_example=example
    )
