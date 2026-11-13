"""The page the digest's 👍/👎 links open, and the dashboard (`job-scout feedback serve`).

GET /f/<job_id>?v=up|down&s=<signature> shows the job, Claude's score, your vote preselected
and a box for a reason; nothing is saved until you press Save (mail apps sometimes open
links on their own). POST /f/<job_id> saves and redirects back to the page.

GET / is the dashboard: every scored job (dashboard.py). It needs no token: it's one
person's LAN page. GET /details/<job_id> is a job's details, which it loads when you open a
row. Its vote dialog posts to POST /f/<job_id> like the page does; its applied checkbox posts
to POST /applied/<job_id>, its "no longer available" checkbox to POST /unavailable/<job_id>.
GET /dashboard.js is its script, the only script this server sends; the dashboard's
Content-Security-Policy allows it, the feedback page's allows none.
GET /d (the dashboard's old, tokened address) redirects to /.

Python's standard HTTP server, a thread per connection: browsers open connections ahead of
time and may send nothing on them, which would hold up a one-at-a-time server until the
timeout. Every write must carry the job's signature (links.py), as does the feedback page;
anything else gets a 403 or 404.
"""

from __future__ import annotations

import gzip
import json
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit
from zoneinfo import ZoneInfo

from jinja2 import ChoiceLoader, Environment, PackageLoader, StrictUndefined, select_autoescape
from pydantic import SecretStr
from sqlalchemy.orm import Session, sessionmaker

from job_scout.config import AppConfig
from job_scout.db.groups import group_of
from job_scout.db.models import Vote
from job_scout.db.types import utcnow
from job_scout.feedback.dashboard import (
    FILTER_VALUES,
    Filters,
    cookie_state,
    dashboard_sections,
    default,
    filters_cookie,
    section_sorts,
)
from job_scout.feedback.links import verify
from job_scout.feedback.store import (
    MAX_REASON_CHARS,
    current_score,
    get_vote,
    record_vote,
    set_applied,
    set_unavailable,
)
from job_scout.report.data import item_mark, scored_item, watchlist_names
from job_scout.report.render import http_url
from job_scout.scoring.points import Breakdown
from job_scout.scoring.schema import CoreResult

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 16 * 1024
REQUEST_TIMEOUT_SECONDS = 15
_PATH = re.compile(r"/f/(\d{1,12})")
_MARK_PATH = re.compile(r"/(applied|unavailable)/(\d{1,12})")
_DETAILS_PATH = re.compile(r"/details/(\d{1,12})")
_DASHBOARD_PREFIXES = ("/applied/", "/unavailable/", "/details/")
_GZIP_MIN_BYTES = 1024
_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
    "frame-ancestors 'none'; base-uri 'none'"
)
# The dashboard runs its own script (served from this origin, never inline) and posts with it.
_DASHBOARD_CSP = f"{_CSP}; script-src 'self'; connect-src 'self'"
_HTML = "text/html; charset=utf-8"
_HEADERS = {
    "Content-Security-Policy": _CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

_ENV = Environment(
    # The report's templates too: the dashboard shows a job's details as the digest does.
    loader=ChoiceLoader(
        [
            PackageLoader("job_scout.feedback", "templates"),
            PackageLoader("job_scout.report", "templates"),
        ]
    ),
    autoescape=select_autoescape(enabled_extensions=("html",), default_for_string=True),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)
_ENV.filters["http_url"] = http_url
_ENV.filters["mark"] = item_mark


@dataclass
class Response:
    status: int
    body: str = ""
    headers: dict[str, str] = field(default_factory=dict)


def _first(values: Mapping[str, list[str]], name: str) -> str:
    return (values.get(name) or [""])[0]


class FeedbackApp:
    """Request handling without sockets, so tests can call `handle` directly."""

    def __init__(
        self,
        config: AppConfig,
        session_factory: sessionmaker[Session],
        secret: SecretStr,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.config = config
        self.sf = session_factory
        self.secret = secret
        self.clock = clock

    def handle(self, method: str, target: str, body: str = "", cookies: str = "") -> Response:
        url = urlsplit(target)
        if url.path in ("/", "/dashboard.js", "/d") or url.path.startswith(_DASHBOARD_PREFIXES):
            return self._dashboard_request(method, url.path, body, cookies)
        match = _PATH.fullmatch(url.path)
        if match is None:
            return self._error(404, "Not found.")
        job_id = int(match.group(1))
        if method == "GET":
            query = parse_qs(url.query)
            signature = _first(query, "s")
            if not verify(self.secret, job_id, signature):
                return self._error(403, "This link isn't valid. Use the links in the digest.")
            return self._page(job_id, signature, _first(query, "v"), saved="saved" in query)
        if method == "POST":
            form = parse_qs(body, keep_blank_values=True)
            signature = _first(form, "s")
            if not verify(self.secret, job_id, signature):
                return self._error(403, "This link isn't valid. Use the links in the digest.")
            try:
                vote = Vote(_first(form, "vote"))
            except ValueError:
                return self._error(400, "Pick 👍 or 👎.")
            return self._save(job_id, signature, vote, _first(form, "reason"))
        return self._error(405, "Method not allowed.")

    def _page(self, job_id: int, signature: str, vote: str, *, saved: bool) -> Response:
        with self.sf() as session:
            group = group_of(session, job_id)
            if group is None or group.canonical.id != job_id:
                return self._error(404, "That job isn't in the database.")
            job = group.canonical
            score = current_score(session, job_id)
            result = CoreResult.model_validate(score.result) if score and score.result else None
            previous = get_vote(session, job_id)
            context: dict[str, Any] = {
                "title": f"Feedback: {job.title}",
                "error": None,
                "saved": saved,
                "job_id": job_id,
                "job_title": job.title,
                "company": job.company or "unknown company",
                "apply_url": job.apply_url or job.canonical_url,
                "score": score.overall_score if result and score else None,
                "disqualified": bool(result and score and score.disqualified),
                "breakdown": (
                    Breakdown.from_json(score.breakdown).line()
                    if score and score.breakdown
                    else None
                ),
                "summary": result.summary if result else None,
                "previous": previous,
                "vote": vote if vote in set(Vote) else (previous.vote if previous else ""),
                "reason": (previous.reason if previous else None) or "",
                "signature": signature,
                "max_reason": MAX_REASON_CHARS,
            }
            html = _ENV.get_template("page.html").render(context)
        return Response(200, html, {"Content-Type": _HTML})

    def _save(self, job_id: int, signature: str, vote: Vote, reason: str) -> Response:
        with self.sf.begin() as session:
            group = group_of(session, job_id)
            if group is None or group.canonical.id != job_id:
                return self._error(404, "That job isn't in the database.")
            record_vote(session, job_id, vote, reason, self.clock())
        log.info("feedback: job %s %s%s", job_id, vote.value, " with a reason" if reason else "")
        # Post/Redirect/Get: reloading the page doesn't resubmit the form.
        location = f"/f/{job_id}?" + urlencode({"s": signature, "saved": "1"})
        return Response(303, "", {"Location": location})

    def _error(self, status: int, message: str) -> Response:
        context = {"title": "job-scout feedback", "error": message}
        html = _ENV.get_template("page.html").render(context)
        return Response(status, html, {"Content-Type": _HTML})

    # --- the dashboard ---------------------------------------------------------------------

    def _dashboard_request(self, method: str, path: str, body: str, cookies: str) -> Response:
        details = _DETAILS_PATH.fullmatch(path)
        if (path in ("/", "/dashboard.js", "/d") or details) and method != "GET":
            return self._error(405, "Method not allowed.")
        if details:
            return self._details(int(details.group(1)))
        if path == "/dashboard.js":
            script = files("job_scout.feedback").joinpath("static/dashboard.js").read_text()
            return Response(200, script, {"Content-Type": "text/javascript; charset=utf-8"})
        if path == "/d":  # earlier digests link to /d?k=<token>
            return Response(308, "", {"Location": "/"})
        if path == "/":
            return self._dashboard(cookies)
        match = _MARK_PATH.fullmatch(path)
        if match is None:
            return self._error(404, "Not found.")
        if method != "POST":
            return self._error(405, "Method not allowed.")
        # A checkbox: /applied/<job_id> or /unavailable/<job_id>, its field named the same.
        name, job_id = match.group(1), int(match.group(2))
        form = parse_qs(body, keep_blank_values=True)
        if not verify(self.secret, job_id, _first(form, "s")):
            return _json(403, {"error": "This job's signature isn't valid."})
        value = _first(form, name)
        if value not in ("0", "1"):
            return _json(400, {"error": f"{name} must be 0 or 1."})
        if name == "applied":
            return self._set_applied(job_id, value == "1")
        return self._set_unavailable(job_id, value == "1")

    def _dashboard(self, cookies: str) -> Response:
        tz = self._tz()
        cookie = filters_cookie(self.config.settings.feedback.base_url)
        state = cookie_state(cookies, cookie)
        filters = Filters.from_state(state)
        with self.sf() as session:
            sections = dashboard_sections(
                session, self.config, self.secret, filters, section_sorts(state)
            )
            context: dict[str, Any] = {
                "title": "job-scout dashboard",
                "sections": sections,
                "total": sum(len(s.rows) for s in sections),
                "filters": filters,
                "filter_values": FILTER_VALUES,
                "filter_default": default,
                "filters_cookie": cookie,
                "settings": self.config.settings.digest,
                "generated_at": self.clock().astimezone(tz).strftime("%Y-%m-%d %H:%M %Z"),
                "max_reason": MAX_REASON_CHARS,
                "local_date": lambda when: f"{when.astimezone(tz):%Y-%m-%d}",
            }
            html = _ENV.get_template("dashboard.html").render(context)
        return Response(
            200, html, {"Content-Type": _HTML, "Content-Security-Policy": _DASHBOARD_CSP}
        )

    def _details(self, job_id: int) -> Response:
        with self.sf() as session:
            item = scored_item(session, job_id, watchlist_names(self.config))
            if item is None:
                return self._error(404, "That job isn't scored.")
            html = _ENV.get_template("details.html").render(item=item)
        return Response(200, html, {"Content-Type": _HTML})

    def _set_applied(self, job_id: int, applied: bool) -> Response:
        now = self.clock()
        with self.sf.begin() as session:
            group = group_of(session, job_id)
            if group is None or group.canonical.id != job_id:
                return _json(404, {"error": "That job isn't in the database."})
            key = set_applied(session, group, applied, now)
        log.info("applied: job %s %s", job_id, "checked" if applied else "unchecked")
        date = f"{now.astimezone(self._tz()):%Y-%m-%d}"
        return _json(200, {"applied": applied, "key": key.text, "date": date})

    def _set_unavailable(self, job_id: int, unavailable: bool) -> Response:
        with self.sf.begin() as session:
            group = group_of(session, job_id)
            if group is None or group.canonical.id != job_id:
                return _json(404, {"error": "That job isn't in the database."})
            when = set_unavailable(session, group, unavailable, self.clock())
            size = len(group.jobs)
        log.info(
            "unavailable: job %s (%d postings) %s",
            job_id,
            size,
            "marked" if unavailable else "unmarked",
        )
        date = f"{when.astimezone(self._tz()):%Y-%m-%d}" if when else None
        return _json(200, {"unavailable": unavailable, "date": date})

    def _tz(self) -> ZoneInfo:
        return ZoneInfo(self.config.settings.schedule.timezone)


def _json(status: int, data: dict[str, Any]) -> Response:
    return Response(status, json.dumps(data), {"Content-Type": "application/json"})


class _Handler(BaseHTTPRequestHandler):
    app: FeedbackApp  # set on the subclass `serve` builds
    timeout = REQUEST_TIMEOUT_SECONDS  # a stalled client can't hold the page forever
    server_version = "job-scout"
    sys_version = ""

    def do_GET(self) -> None:
        self._send(self.app.handle("GET", self.path, cookies=self.headers.get("Cookie") or ""))

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY_BYTES:
            self._send(Response(413 if length > 0 else 400, "request body too large or invalid"))
            return
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        self._send(self.app.handle("POST", self.path, body))

    def _send(self, response: Response) -> None:
        data = response.body.encode("utf-8")
        headers = {**_HEADERS, **response.headers}
        # The dashboard is megabytes of repetitive HTML: gzip makes it a tenth of that.
        if len(data) >= _GZIP_MIN_BYTES and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            data = gzip.compress(data, compresslevel=5)
            headers |= {"Content-Encoding": "gzip", "Vary": "Accept-Encoding"}
        self.send_response(response.status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        # The default line has the full URL; keep the signature out of the logs.
        log.debug("%s %s %s", self.command, urlsplit(self.path).path, args[1] if args else "")


def make_server(app: FeedbackApp, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("FeedbackHandler", (_Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True  # an idle connection never holds up shutdown
    return server
