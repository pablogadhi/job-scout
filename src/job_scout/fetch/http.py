"""The only place that talks to the network.

Every request goes through `HttpClient.get`, which:
- sends the configured User-Agent and timeout,
- checks robots.txt (cached per host for the client's lifetime),
- spaces requests to the same host by max(1s, robots Crawl-delay, the source's own interval),
- enforces the source's request budget (e.g. Remotive's 4 calls per day),
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


class FetchError(Exception):
    """A source could not be fetched. The message is shown in source health."""


class RobotsDisallowed(FetchError):
    pass


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
        host = self._host(full_url)
        robots = host.robots if self.defaults.respect_robots_txt else None
        if robots is not None and not robots.can_fetch(self.defaults.user_agent, full_url):
            raise RobotsDisallowed(f"robots.txt disallows {full_url}")
        budget.spend(full_url)
        self._wait(host, source)
        log.debug("GET %s", full_url)
        try:
            response = self.client.get(full_url, headers=headers)
        except httpx.HTTPError as exc:
            raise FetchError(f"{type(exc).__name__} for {full_url}: {exc}") from exc
        finally:
            host.last_request = self.clock()
        if self.recorder:
            self.recorder.record(source.id, full_url, response)
        if response.status_code >= 400:
            raise FetchError(f"HTTP {response.status_code} for {full_url}")
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
        if response.status_code in (401, 403):
            parser.parse(["User-agent: *", "Disallow: /"])
        elif response.status_code >= 500:
            raise FetchError(f"{origin}/robots.txt returned HTTP {response.status_code}")
        elif response.status_code >= 400:
            parser.parse([])  # no robots.txt: everything allowed
        else:
            parser.parse(response.text.splitlines())
        return parser

    def _wait(self, host: _Host, source: SourceConfig) -> None:
        interval = MIN_HOST_INTERVAL_SECONDS
        if host.robots is not None:
            interval = max(interval, float(host.robots.crawl_delay(self.defaults.user_agent) or 0))
        if source.min_request_interval_seconds:
            interval = max(interval, source.min_request_interval_seconds)
        if host.last_request is not None:
            remaining = interval - (self.clock() - host.last_request)
            if remaining > 0:
                self.sleep(remaining)


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
