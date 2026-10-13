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
    Recorder,
    RequestBudget,
    RobotsDisallowed,
    forbidden,
)


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
