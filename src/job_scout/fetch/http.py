"""The only place that talks to the network.

Every request goes through `HttpClient.get`, which:
- sends the configured User-Agent and timeout,
- checks robots.txt (cached per host for the client's lifetime),
- spaces requests to the same host by max(1s, robots Crawl-delay, the source's own interval),
- enforces the source's request budget (e.g. Remotive's 4 calls per day),
- on HTTP 429, waits (Retry-After, or a short backoff), retries, and slows down for that host,
- optionally records responses as test fixtures (`fetch --record DIR`).
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from job_scout.config.sources import SourceConfig, SourceDefaults

log = logging.getLogger(__name__)

MIN_HOST_INTERVAL_SECONDS = 1.0
RATE_LIMIT_RETRIES = 2  # extra attempts after an HTTP 429
RATE_LIMIT_BACKOFF_SECONDS = 5.0  # first wait when there's no Retry-After; doubles per retry
RATE_LIMIT_MAX_WAIT_SECONDS = 60.0  # a longer Retry-After isn't waited for: the request fails
RATE_LIMIT_SLOWDOWN_SECONDS = (2.0, 10.0)  # after a 429: per-host spacing, doubling up to max

# PLAN.md ground rule: never fetch these, whoever asks (a seed company's careers link can
# point at them). Subdomains included.
FORBIDDEN_DOMAINS = ("linkedin.com", "indeed.com", "wellfound.com", "angel.co", "google.com")


def forbidden(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == d or host.endswith(f".{d}") for d in FORBIDDEN_DOMAINS)


class FetchError(Exception):
    """A source could not be fetched. The message is shown in source health.

    `retryable`: the failure is likely temporary (rate limit, server error, timeout), so
    trying again later may succeed.
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class RobotsDisallowed(FetchError):
    pass


class ForbiddenHost(FetchError):
    pass


class HttpStatusError(FetchError):
    """The server answered with an error status (4xx/5xx)."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message, retryable=status_code == 429 or status_code >= 500)
        self.status_code = status_code


class BudgetExhausted(FetchError):
    pass


@dataclass
class RequestBudget:
    """Requests a source may still make. None means unlimited."""

    remaining: int | None = None
    used: int = 0

    def spend(self, url: str) -> None:
        if self.remaining is not None and self.used >= self.remaining:
            raise BudgetExhausted(f"daily request budget used up before {url}")
        self.used += 1


@dataclass
class _Host:
    robots: RobotFileParser | None = None
    last_request: float | None = None
    slowdown: float = 0.0  # extra spacing after this host rate-limited us (HTTP 429)


@dataclass
class HttpClient:
    defaults: SourceDefaults
    transport: httpx.BaseTransport | None = None
    recorder: Recorder | None = None
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    _hosts: dict[str, _Host] = field(default_factory=dict)
    _client: httpx.Client | None = None

    def __enter__(self) -> HttpClient:
        self._client = httpx.Client(
            headers={"User-Agent": self.defaults.user_agent},
            timeout=self.defaults.timeout_seconds,
            follow_redirects=True,
            transport=self.transport,
        )
        return self

    def __exit__(self, *exc: object) -> None:
        if self._client:
            self._client.close()

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            raise RuntimeError("HttpClient used outside its context manager")
        return self._client

    def get(
        self,
        url: str,
        *,
        source: SourceConfig,
        budget: RequestBudget,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        # Merge, don't replace: endpoints in sources.yaml carry their own query strings.
        full_url = str(httpx.URL(url).copy_merge_params(params)) if params else url
        if forbidden(full_url):
            raise ForbiddenHost(f"never fetched (ground rule): {full_url}")
        host = self._host(full_url)
        robots = host.robots if self.defaults.respect_robots_txt else None
        if robots is not None and not robots.can_fetch(self.defaults.user_agent, full_url):
            raise RobotsDisallowed(f"robots.txt disallows {full_url}")
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            budget.spend(full_url)
            self._wait(host, source)
            log.debug("GET %s", full_url)
            try:
                response = self.client.get(full_url, headers=headers)
            except httpx.HTTPError as exc:
                raise FetchError(
                    f"{type(exc).__name__} for {full_url}: {exc}",
                    retryable=isinstance(exc, httpx.TimeoutException),
                ) from exc
            finally:
                host.last_request = self.clock()
            if response.status_code != 429 or attempt == RATE_LIMIT_RETRIES:
                break
            delay = _retry_after(response) or RATE_LIMIT_BACKOFF_SECONDS * 2**attempt
            if delay > RATE_LIMIT_MAX_WAIT_SECONDS:
                break
            low, high = RATE_LIMIT_SLOWDOWN_SECONDS
            host.slowdown = min(max(host.slowdown * 2, low), high)
            log.info(
                "HTTP 429 from %s: retrying in %.0fs, then %.0fs between requests",
                urlsplit(full_url).netloc,
                delay,
                host.slowdown,
            )
            self.sleep(delay)
        if self.recorder:
            self.recorder.record(source.id, full_url, response)
        if response.status_code >= 400:
            raise HttpStatusError(
                f"HTTP {response.status_code} for {full_url}", response.status_code
            )
        return response

    def _host(self, url: str) -> _Host:
        parts = urlsplit(url)
        key = f"{parts.scheme}://{parts.netloc}"
        if key not in self._hosts:
            self._hosts[key] = _Host(
                robots=self._load_robots(key) if self.defaults.respect_robots_txt else None
            )
        return self._hosts[key]

    def _load_robots(self, origin: str) -> RobotFileParser:
        parser = RobotFileParser(f"{origin}/robots.txt")
        try:
            response = self.client.get(f"{origin}/robots.txt")
        except httpx.HTTPError as exc:
            raise FetchError(f"could not read {origin}/robots.txt: {exc}") from exc
        # RFC 9309 2.3.1: any 4xx (401/403 included) means robots.txt is unavailable, so
        # everything is allowed; a server error means assume everything is disallowed, which
        # failing the request does. (urllib.robotparser treats 401/403 as disallow-all.)
        if response.status_code >= 500:
            raise HttpStatusError(
                f"{origin}/robots.txt returned HTTP {response.status_code}", response.status_code
            )
        if response.status_code >= 400:
            parser.parse([])
        else:
            parser.parse(response.text.splitlines())
        return parser

    def _wait(self, host: _Host, source: SourceConfig) -> None:
        interval = max(MIN_HOST_INTERVAL_SECONDS, host.slowdown)
        if host.robots is not None:
            interval = max(interval, float(host.robots.crawl_delay(self.defaults.user_agent) or 0))
        if source.min_request_interval_seconds:
            interval = max(interval, source.min_request_interval_seconds)
        if host.last_request is not None:
            remaining = interval - (self.clock() - host.last_request)
            if remaining > 0:
                self.sleep(remaining)


def _retry_after(response: httpx.Response) -> float | None:
    """Retry-After in seconds (the HTTP-date form is ignored: the backoff applies)."""
    try:
        return max(0.0, float(response.headers.get("retry-after", "")))
    except ValueError:
        return None


class Recorder:
    """Saves responses under DIR/<source_id>/ with a manifest mapping URL -> file.

    Large payloads are trimmed to `max_items` items so fixtures stay small; pagination
    fields are left as they were, so every recorded page is needed to replay a fetch.
    """

    def __init__(self, directory: Path, max_items: int = 5) -> None:
        self.directory = directory
        self.max_items = max_items

    def record(self, source_id: str, url: str, response: httpx.Response) -> None:
        target = self.directory / source_id
        target.mkdir(parents=True, exist_ok=True)
        manifest_path = target / "manifest.json"
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
        content_type = response.headers.get("content-type", "")
        is_json = "json" in content_type
        name = f"{len(manifest) + 1:02d}-{_slug(url)}.{'json' if is_json else 'xml'}"
        body = _trim_json(response.content, self.max_items) if is_json else response.content
        if not is_json and response.status_code < 400:
            body = _trim_feed(body, self.max_items)
        (target / name).write_bytes(body)
        manifest[url] = {
            "file": name,
            "status": response.status_code,
            "content_type": content_type,
        }
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")


def _slug(url: str) -> str:
    parts = urlsplit(url)
    return re.sub(r"[^a-z0-9]+", "-", f"{parts.path} {parts.query}".lower()).strip("-")[:60]


def _trim_json(content: bytes, max_items: int) -> bytes:
    try:
        data = json.loads(content)
    except ValueError:
        return content

    def trim(node: Any) -> Any:
        if isinstance(node, list):
            return node[: max_items + 1]  # +1: Remote OK's first element is metadata
        if isinstance(node, dict):
            return {
                k: trim(v) if isinstance(v, list) and len(v) > max_items else v
                for k, v in node.items()
            }
        return node

    return (json.dumps(trim(data), indent=1, ensure_ascii=False) + "\n").encode()


def _trim_feed(content: bytes, max_items: int) -> bytes:
    """Keep the first `max_items` <item>/<entry> elements of an RSS or Atom feed."""
    for tag in (b"item", b"entry"):
        starts = [m.start() for m in re.finditer(rb"<" + tag + rb"[\s>]", content)]
        if len(starts) > max_items:
            closing = b"</" + tag + b">"
            last_end = content.rfind(closing) + len(closing)
            cut = starts[max_items]
            return content[:cut] + content[last_end:]
    return content
