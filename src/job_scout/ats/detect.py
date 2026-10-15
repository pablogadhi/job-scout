"""Find a company's job board, in the order sources.yaml gives (stop at the first hit):

1. careers_url: the URL itself is a board (its host matches a platform's `detect` pattern).
2. careers_page: the careers page (or, without one, the website and its careers link)
   links to or embeds a board.
3. probe: the company slug answers on a platform's API with at least one job.
A board found by 1 or 2 must answer on its API too (with any number of jobs): a careers URL
or page can still point to a board the company has since deleted.
Otherwise the company is unknown_ats, unless a check failed for a temporary reason (rate
limit, server error, timeout): then the result is inconclusive (`retry`) and the company is
detected again next run (store.save_detection caps the attempts).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

from job_scout.ats.platforms import PLATFORMS, api_url, tokens_in
from job_scout.ats.seed import SeedCompany
from job_scout.config.sources import AtsPlatform, SourceConfig
from job_scout.db.models import AtsMethod
from job_scout.fetch.http import (
    FetchError,
    HttpClient,
    HttpStatusError,
    RequestBudget,
    RobotsDisallowed,
)

log = logging.getLogger(__name__)

_CAREERS_LINK = re.compile(
    r"""href=["']([^"'#]*(?:careers?|jobs|join-us|work-with-us|hiring)[^"'#]*)["']""", re.I
)
MAX_PAGE_CHARS = 2_000_000


@dataclass(frozen=True)
class Detection:
    ats: str | None = None
    token: str | None = None
    method: AtsMethod | None = None
    note: str | None = None  # why nothing was found, or what failed along the way
    retry: bool = False  # nothing found, but a check failed temporarily: try again later


def find_board(text: str, platforms: Mapping[str, AtsPlatform]) -> tuple[str, str] | None:
    """The first (ats, token) any configured platform's token pattern finds in the text."""
    for name in platforms:
        for token in tokens_in(text, PLATFORMS[name]):
            return name, token
    return None


def board_from_url(url: str | None, platforms: Mapping[str, AtsPlatform]) -> tuple[str, str] | None:
    if not url:
        return None
    host = urlsplit(url if "://" in url else f"https://{url}").netloc
    for name, platform in platforms.items():
        if re.search(platform.detect, host, re.I):
            for token in tokens_in(url if "://" in url else f"https://{url}", PLATFORMS[name]):
                return name, token
    return None


def slug_candidates(company: SeedCompany) -> list[str]:
    """The seed slug, then the website's main domain label (acme.com -> acme)."""
    candidates = [company.slug]
    if company.website:
        host = urlsplit(company.website if "://" in company.website else f"//{company.website}")
        labels = [p for p in host.netloc.lower().split(".") if p and p != "www"]
        if len(labels) >= 2:
            candidates.append(labels[-2])
    return list(dict.fromkeys(c for c in candidates if re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", c)))


class Detector:
    def __init__(
        self,
        http: HttpClient,
        source: SourceConfig,
        platforms: Mapping[str, AtsPlatform],
        budget: RequestBudget | None = None,
    ) -> None:
        self.http = http
        self.source = source
        self.platforms = dict(platforms)
        self.budget = budget or RequestBudget()
        self._blocked: set[str] = set()  # platforms whose API robots.txt disallows us
        self._inconclusive = False  # this company: a check failed temporarily

    def detect(self, company: SeedCompany) -> Detection:
        self._inconclusive = False
        notes: list[str] = []
        hit = board_from_url(company.careers_url, self.platforms)
        if hit and self._answers(hit, notes):
            return Detection(*hit, AtsMethod.CAREERS_URL)
        if (hit := self._from_pages(company, notes)) and self._answers(hit, notes):
            return Detection(*hit, AtsMethod.CAREERS_PAGE)
        if hit := self._probe(company, notes):
            return Detection(*hit, AtsMethod.PROBE)
        return Detection(note="; ".join(notes) or "no board found", retry=self._inconclusive)

    def _failed(self, notes: list[str], note: str, exc: FetchError) -> None:
        notes.append(note)
        self._inconclusive = self._inconclusive or exc.retryable

    def _page(self, url: str, notes: list[str]) -> str | None:
        try:
            response = self.http.get(url, source=self.source, budget=self.budget)
        except FetchError as exc:
            self._failed(notes, f"{url}: {exc}", exc)
            return None
        return response.text[:MAX_PAGE_CHARS]

    def _from_pages(self, company: SeedCompany, notes: list[str]) -> tuple[str, str] | None:
        start = company.careers_url or company.website
        if not start:
            return None
        start = start if "://" in start else f"https://{start}"
        html = self._page(start, notes)
        if html is None:
            return None
        if hit := find_board(html, self.platforms):
            return hit
        if company.careers_url:
            return None
        # No careers URL: follow the website's first same-site careers link, once.
        site = urlsplit(start).netloc.removeprefix("www.")
        for match in _CAREERS_LINK.finditer(html):
            link = urljoin(start, match.group(1))
            if hit := board_from_url(link, self.platforms):
                return hit
            if urlsplit(link).netloc.removeprefix("www.") == site and link != start:
                page = self._page(link, notes)
                return find_board(page, self.platforms) if page else None
        return None

    def _jobs(self, name: str, token: str, notes: list[str]) -> list[Any] | None:
        """The board's jobs from its API, or None when it doesn't answer (a 404 is no note:
        probing expects them)."""
        if name in self._blocked:
            return None
        url = api_url(self.platforms[name].api, token)
        try:
            payload = self.http.get(url, source=self.source, budget=self.budget).json()
            return PLATFORMS[name].jobs(payload)
        except RobotsDisallowed as exc:
            self._blocked.add(name)
            notes.append(f"{name} API off: {exc}")
        except HttpStatusError as exc:
            if exc.status_code != 404:
                self._failed(notes, f"{name}:{token}: {exc}", exc)
        except FetchError as exc:
            self._failed(notes, f"{name}:{token}: {exc}", exc)
        except ValueError as exc:
            notes.append(f"{name}:{token}: {exc}")
        return None

    def _answers(self, hit: tuple[str, str], notes: list[str]) -> bool:
        """A board found in a URL or page exists: its API answers, even with no jobs."""
        if self._jobs(*hit, notes) is not None:
            return True
        notes.append(f"{hit[0]}:{hit[1]} doesn't answer on its API")
        return False

    def _probe(self, company: SeedCompany, notes: list[str]) -> tuple[str, str] | None:
        for candidate in slug_candidates(company):
            for name in self.platforms:
                if self._jobs(name, candidate, notes):
                    return name, candidate
        return None
