"""The remoteintech seed: the remote-jobs repo as a tarball, one Markdown file per company.

Files are read from the archive in memory, never extracted to disk, and size-capped.
"""

from __future__ import annotations

import io
import logging
import re
import tarfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import yaml

log = logging.getLogger(__name__)

_COMPANY_FILE = re.compile(r"(?:^|/)src/companies/[^/]+\.md$")
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.S)
MAX_FILE_BYTES = 256 * 1024
MAX_FILES = 10_000


@dataclass(frozen=True)
class SeedCompany:
    slug: str
    name: str
    website: str | None
    careers_url: str | None
    region: str | None
    remote_policy: str | None


def _text(value: object) -> str | None:
    return str(value).strip() or None if value is not None else None


def _url(value: object) -> str | None:
    """Seed URLs are hand-written: cut a pasted Markdown link ("…/career](https://…") at the
    bracket, keep the first of several, and drop trailing punctuation ("…/careers/,")."""
    text = _text(value)
    if not text:
        return None
    text = re.split(r"\]\(|\s|,(?=\s*https?://)", text)[0]
    return text.rstrip(".,;)]") or None


def parse_company(filename: str, text: str) -> SeedCompany | None:
    match = _FRONTMATTER.match(text)
    if not match:
        return None
    data = yaml.safe_load(match.group(1))
    if not isinstance(data, dict):
        return None
    slug = _text(data.get("slug")) or filename.rsplit("/", 1)[-1].removesuffix(".md")
    name = _text(data.get("title")) or slug
    return SeedCompany(
        slug=slug.lower(),
        name=name,
        website=_url(data.get("website")),
        careers_url=_url(data.get("careers_url")),
        region=_text(data.get("region")),
        remote_policy=_text(data.get("remote_policy")),
    )


def parse_seed(tarball: bytes) -> list[SeedCompany]:
    """Every company file in the archive; a malformed file is logged and skipped."""
    companies: list[SeedCompany] = []
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:*") as tar:
        for member in tar:
            if not (member.isfile() and _COMPANY_FILE.search(member.name)):
                continue
            if member.size > MAX_FILE_BYTES or len(companies) >= MAX_FILES:
                log.warning("seed: skipped %s (too large or too many files)", member.name)
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            try:
                company = parse_company(member.name, handle.read().decode("utf-8"))
            except (UnicodeDecodeError, yaml.YAMLError) as exc:
                log.warning("seed: skipped %s: %s", member.name, exc)
                continue
            if company is None:
                log.warning("seed: skipped %s: no frontmatter", member.name)
                continue
            companies.append(company)
    return companies


def keep(
    companies: Iterable[SeedCompany], regions: Sequence[str], policies: Sequence[str]
) -> list[SeedCompany]:
    """Companies in the allowed regions and remote policies (an empty list allows all)."""
    return [
        c
        for c in companies
        if (not regions or c.region in regions) and (not policies or c.remote_policy in policies)
    ]
