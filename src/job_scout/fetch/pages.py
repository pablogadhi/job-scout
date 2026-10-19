"""Job pages: the schema.org JobPosting many boards embed as JSON-LD (title, description,
jobLocation, jobLocationType, applicantLocationRequirements), read by company-board
platforms and by sources whose feed carries only a summary."""

from __future__ import annotations

import json
import re
from typing import Any

_JSON_LD = re.compile(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", re.S | re.I)


def job_posting_ld(page: str) -> dict[str, Any] | None:
    """The page's schema.org JobPosting, if it has one."""
    for block in _JSON_LD.findall(page):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return None


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value] if value else []


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def ld_place(place: Any) -> str | None:
    """'Ghent, BE' from a jobLocation Place."""
    address = as_dict(place).get("address")
    if isinstance(address, str):
        return address
    address = as_dict(address)
    country = address.get("addressCountry")
    if isinstance(country, dict):
        country = as_dict(country).get("name")
    parts = [address.get("addressLocality"), address.get("addressRegion"), country]
    return ", ".join(str(p) for p in parts if p and p != "UNAVAILABLE") or None


def ld_locations(ld: dict[str, Any]) -> list[str]:
    """The job's places, then the countries applicants must be in, worded so the location
    pre-filter and Claude both read them as a residency rule."""
    places = [p for p in (ld_place(p) for p in as_list(ld.get("jobLocation"))) if p]
    countries = [
        str(a["name"])
        for a in as_list(ld.get("applicantLocationRequirements"))
        if isinstance(a, dict) and a.get("name")
    ]
    if countries:
        if ld.get("jobLocationType") == "TELECOMMUTE":
            # A remote job's places are where applicants may be; typescriptjobs names only
            # the first of them as the requirement.
            countries, places = [*countries, *places], []
        places.append(f"must be based in {' or '.join(dict.fromkeys(countries))}")
    return places
