import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from job_scout.config import AppConfig
from job_scout.fetch.http import (
    BudgetExhausted,
    FetchError,
    ForbiddenHost,
    HttpClient,
    HttpStatusError,
    Recorder,
    RequestBudget,
    RobotsDisallowed,
    forbidden,
    request_key,
)
from tests.conftest import fixture_transport


def ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True})


def robots(text: str, status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _request: httpx.Response(status, text=text)


def test_sends_user_agent(app_config: AppConfig, make_http: Callable[..., HttpClient]) -> None:
    agents: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        agents.append(request.headers["user-agent"])
        return ok(request)

    source = app_config.sources.get("remoteok")
    with make_http({"https://example.com/api": handler}) as http:
        http.get("https://example.com/api", source=source, budget=RequestBudget())
    assert agents == [app_config.sources.defaults.user_agent]


def test_robots_disallow_blocks_request(
    app_config: AppConfig, make_http: Callable[..., HttpClient]
) -> None:
    source = app_config.sources.get("remoteok")
    overrides = {
        "https://example.com/robots.txt": robots("User-agent: *\nDisallow: /private"),
        "https://example.com/": ok,
    }
    with make_http(overrides) as http:
        http.get("https://example.com/public", source=source, budget=RequestBudget())
        with pytest.raises(RobotsDisallowed):
            http.get("https://example.com/private/x", source=source, budget=RequestBudget())


def test_ignore_robots_txt_skips_only_the_disallow_rules(
    app_config: AppConfig, make_http: Callable[..., HttpClient]
) -> None:
    waits: list[float] = []
    exempt = app_config.sources.get("reliefweb")  # ignore_robots_txt: true
    assert exempt.ignore_robots_txt
    overrides = {
        "https://example.com/robots.txt": robots("User-agent: *\nDisallow: /\nCrawl-delay: 3"),
        "https://example.com/": ok,
    }
    with make_http(overrides, sleep=waits.append) as http:
        with pytest.raises(RobotsDisallowed):
            http.get(
                "https://example.com/feed",
                source=app_config.sources.get("remoteok"),
                budget=RequestBudget(),
            )
        for _ in range(2):
            http.get("https://example.com/feed", source=exempt, budget=RequestBudget())
    assert waits and waits[-1] > 2  # Crawl-delay still spaces the requests


WILDCARD_ROBOTS = """User-agent: *
Disallow: /api/*
Allow: /api/public/
Disallow: /*category=
Disallow: /*/rss.xml
Disallow: /exact$
"""


@pytest.mark.parametrize(
    ("path", "allowed"),
    [
        ("/api/jobs", False),
        ("/api/public/jobs", True),  # the longest matching rule wins
        ("/jobs?category=a", False),
        ("/jobs/rss.xml", False),
        ("/exact", False),
        ("/exactly", True),  # $ anchors the end
        ("/remote-jobs/feed", True),
    ],
)
def test_robots_wildcards(
    app_config: AppConfig, make_http: Callable[..., HttpClient], path: str, allowed: bool
) -> None:
    """RFC 9309 patterns: `*` matches any run of characters, `$` the end of the path."""
    source = app_config.sources.get("remoteok")
    overrides = {
        "https://example.com/robots.txt": robots(WILDCARD_ROBOTS),
        "https://example.com/": ok,
    }
    with make_http(overrides) as http:
        if allowed:
            http.get(f"https://example.com{path}", source=source, budget=RequestBudget())
        else:
            with pytest.raises(RobotsDisallowed):
                http.get(f"https://example.com{path}", source=source, budget=RequestBudget())


@pytest.mark.parametrize(
    "text",
    [
        "﻿User-agent: *\nDisallow: /private",  # leading byte-order mark
        "User-agent: job-scout\nDisallow: /private\n\nUser-agent: *\nDisallow: /",  # our group
    ],
)
def test_robots_groups(
    app_config: AppConfig, make_http: Callable[..., HttpClient], text: str
) -> None:
    source = app_config.sources.get("remoteok")
    overrides = {
        "https://example.com/robots.txt": lambda _r: httpx.Response(200, content=text.encode()),
        "https://example.com/": ok,
    }
    with make_http(overrides) as http:
        http.get("https://example.com/public", source=source, budget=RequestBudget())
        with pytest.raises(RobotsDisallowed):
            http.get("https://example.com/private/x", source=source, budget=RequestBudget())


@pytest.mark.parametrize("status", [404, 401, 403, 410])
def test_robots_4xx_means_allowed(
    app_config: AppConfig, make_http: Callable[..., HttpClient], status: int
) -> None:
    """RFC 9309: robots.txt answering any 4xx is "unavailable", so everything is allowed."""
    source = app_config.sources.get("remoteok")
    overrides = {"https://example.com/robots.txt": robots("", status), "https://example.com/": ok}
    with make_http(overrides) as http:
        http.get("https://example.com/a", source=source, budget=RequestBudget())


def test_robots_5xx_fails_the_request(
    app_config: AppConfig, make_http: Callable[..., HttpClient]
) -> None:
    source = app_config.sources.get("remoteok")
    overrides = {"https://example.com/robots.txt": robots("", 503), "https://example.com/": ok}
    with make_http(overrides) as http, pytest.raises(FetchError, match="HTTP 503"):
        http.get("https://example.com/a", source=source, budget=RequestBudget())


def test_spacing_uses_crawl_delay_and_source_interval(
    app_config: AppConfig, make_http: Callable[..., HttpClient]
) -> None:
    slept: list[float] = []
    overrides = {
        "https://example.com/robots.txt": robots("User-agent: *\nCrawl-delay: 3"),
        "https://example.com/": ok,
    }
    remoteok = app_config.sources.get("remoteok")
    remotive = app_config.sources.get("remotive")  # min_request_interval_seconds: 31
    with make_http(overrides, sleep=slept.append) as http:
        for source in (remoteok, remoteok, remotive):
            http.get("https://example.com/a", source=source, budget=RequestBudget())
    assert slept[0] == pytest.approx(3, abs=0.5)
    assert slept[1] == pytest.approx(31, abs=0.5)


def test_budget(app_config: AppConfig, make_http: Callable[..., HttpClient]) -> None:
    source = app_config.sources.get("remotive")
    budget = RequestBudget(remaining=1)
    with make_http({"https://example.com/": ok}) as http:
        http.get("https://example.com/a", source=source, budget=budget)
        with pytest.raises(BudgetExhausted):
            http.get("https://example.com/b", source=source, budget=budget)
    assert budget.used == 1


def test_http_errors_become_fetch_errors(
    app_config: AppConfig, make_http: Callable[..., HttpClient]
) -> None:
    source = app_config.sources.get("remoteok")

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    overrides = {"https://example.com/500": lambda _r: httpx.Response(500), "https://x.org/": boom}
    with make_http(overrides) as http:
        with pytest.raises(FetchError, match="HTTP 500"):
            http.get("https://example.com/500", source=source, budget=RequestBudget())
        # Unreachable host: the robots.txt read is the first request, and it fails cleanly.
        with pytest.raises(FetchError, match=r"x\.org/robots\.txt: refused"):
            http.get("https://x.org/a", source=source, budget=RequestBudget())


def test_params_merge_into_endpoint_query(
    app_config: AppConfig, make_http: Callable[..., HttpClient]
) -> None:
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return ok(request)

    source = app_config.sources.get("4dayweek")
    with make_http({"https://example.com/": handler}) as http:
        http.get(
            "https://example.com/a?limit=100",
            source=source,
            budget=RequestBudget(),
            params={"page": 2},
        )
    assert urls[-1] == "https://example.com/a?limit=100&page=2"


def test_recorder_trims_payloads(tmp_path: Path) -> None:
    recorder = Recorder(tmp_path, max_items=2)
    rss = (
        b"<rss><channel>" + b"".join(b"<item>%d</item>" % i for i in range(5)) + b"</channel></rss>"
    )
    recorder.record(
        "src",
        "https://e.com/feed",
        httpx.Response(200, headers={"content-type": "application/rss+xml"}, content=rss),
    )
    recorder.record(
        "src", "https://e.com/api", httpx.Response(200, json={"data": list(range(10)), "page": 1})
    )
    manifest = json.loads((tmp_path / "src" / "manifest.json").read_text())
    feed = (tmp_path / "src" / manifest["https://e.com/feed"]["file"]).read_bytes()
    assert feed == b"<rss><channel><item>0</item><item>1</item></channel></rss>"
    api = json.loads((tmp_path / "src" / manifest["https://e.com/api"]["file"]).read_text())
    assert api == {"data": [0, 1, 2], "page": 1}  # max_items + 1 (Remote OK's metadata row)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/company/acme/jobs",
        "https://linkedin.com/jobs",
        "https://angel.co/company/acme/jobs",
        "https://wellfound.com/company/acme",
        "https://uk.indeed.com/cmp/acme",
        "https://www.google.com/search?q=jobs",
    ],
)
def test_forbidden_hosts_are_never_fetched(
    app_config: AppConfig, make_http: Callable[..., HttpClient], url: str
) -> None:
    def explode(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may reach a forbidden host, not even robots.txt")

    with make_http({"https://": explode}) as http, pytest.raises(ForbiddenHost):
        http.get(url, source=app_config.sources.get("remoteok"), budget=RequestBudget())


def test_lookalike_hosts_are_allowed() -> None:
    assert not forbidden("https://notlinkedin.com/jobs")
    assert not forbidden("https://google.com.example/x")


class FakeTime:
    """sleep() advances clock(), as real time would."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 3))
        self.now += seconds

    def clock(self) -> float:
        return self.now


def test_rate_limit_waits_retries_and_slows_down(app_config: AppConfig) -> None:
    source = app_config.sources.get("remoteok")
    answers = iter([httpx.Response(429, headers={"Retry-After": "3"}), httpx.Response(429)])

    def limited(request: httpx.Request) -> httpx.Response:
        return next(answers, None) or ok(request)

    time = FakeTime()
    overrides = {"https://example.com/a": limited, "https://example.com/b": ok}
    transport = fixture_transport(overrides)
    budget = RequestBudget()
    defaults = app_config.sources.defaults
    with HttpClient(defaults, transport=transport, sleep=time.sleep, clock=time.clock) as http:
        response = http.get("https://example.com/a", source=source, budget=budget)
        assert response.status_code == 200 and budget.used == 3
        # Retry-After 3s, then the backoff (5s doubled on the second retry).
        assert time.sleeps == [3.0, 10.0]
        http.get("https://example.com/b", source=source, budget=budget)
        assert time.sleeps[-1] == 4.0  # this host's spacing: 2s, doubled by the second 429


def test_rate_limit_that_persists_is_a_retryable_error(
    app_config: AppConfig, make_http: Callable[..., HttpClient]
) -> None:
    source = app_config.sources.get("remoteok")
    sleeps: list[float] = []
    overrides = {
        "https://example.com/busy": lambda _r: httpx.Response(429),
        "https://example.com/later": lambda _r: httpx.Response(429, headers={"Retry-After": "900"}),
        "https://example.com/down": lambda _r: httpx.Response(503),
        "https://example.com/gone": lambda _r: httpx.Response(404),
    }
    with make_http(overrides, sleep=sleeps.append) as http:
        with pytest.raises(HttpStatusError, match="HTTP 429") as busy:
            http.get("https://example.com/busy", source=source, budget=RequestBudget())
        assert busy.value.retryable
        sleeps.clear()
        budget = RequestBudget()
        with pytest.raises(HttpStatusError, match="HTTP 429"):
            http.get("https://example.com/later", source=source, budget=budget)
        assert budget.used == 1 and all(s < 60 for s in sleeps)  # too long to wait for
        with pytest.raises(HttpStatusError) as down:
            http.get("https://example.com/down", source=source, budget=RequestBudget())
        assert down.value.retryable
        with pytest.raises(HttpStatusError) as gone:
            http.get("https://example.com/gone", source=source, budget=RequestBudget())
        assert not gone.value.retryable


def test_timeouts_are_retryable(
    app_config: AppConfig, make_http: Callable[..., HttpClient]
) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    source = app_config.sources.get("remoteok")
    overrides = {"https://example.com/slow": slow, "https://example.com/refused": refused}
    with make_http(overrides) as http:
        with pytest.raises(FetchError) as timeout:
            http.get("https://example.com/slow", source=source, budget=RequestBudget())
        assert timeout.value.retryable
        with pytest.raises(FetchError) as connect:
            http.get("https://example.com/refused", source=source, budget=RequestBudget())
        assert not connect.value.retryable


def test_post_sends_json_with_every_check(
    app_config: AppConfig, make_http: Callable[..., HttpClient]
) -> None:
    sent: list[tuple[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append((request.method, json.loads(request.content)))
        return ok(request)

    source = app_config.sources.get("remoteok")
    overrides = {
        "https://example.com/robots.txt": robots("User-agent: *\nDisallow: /private"),
        "https://example.com/search": handler,
    }
    budget = RequestBudget(remaining=1)
    with make_http(overrides) as http:
        response = http.post(
            "https://example.com/search", source=source, budget=budget, json={"offset": 20}
        )
        assert response.json() == {"ok": True}
        with pytest.raises(RobotsDisallowed):
            http.post("https://example.com/private", source=source, budget=budget, json={})
        with pytest.raises(BudgetExhausted):
            http.post("https://example.com/search", source=source, budget=budget, json={})
    assert sent == [("POST", {"offset": 20})]


def test_recorder_keys_posts_by_body(app_config: AppConfig, tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"offset": json.loads(request.content)["offset"]})

    recorder = Recorder(tmp_path)
    source = app_config.sources.get("remoteok")
    transport = fixture_transport({"https://example.com/jobs": handler})
    with HttpClient(
        app_config.sources.defaults, transport=transport, recorder=recorder, sleep=lambda _: None
    ) as http:
        for offset in (0, 20):
            http.post(
                "https://example.com/jobs",
                source=source,
                budget=RequestBudget(),
                json={"offset": offset, "limit": 20},
            )
    manifest = json.loads((tmp_path / "remoteok" / "manifest.json").read_text())
    assert list(manifest) == [
        'POST https://example.com/jobs {"limit":20,"offset":0}',
        'POST https://example.com/jobs {"limit":20,"offset":20}',
    ]
    assert request_key("GET", "https://example.com/jobs") == "https://example.com/jobs"
