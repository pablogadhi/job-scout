"""The page the digest's 👍/👎 links open (`job-scout feedback serve`).

GET /f/<job_id>?v=up|down&s=<signature> shows the job, Claude's score, your vote preselected
and a box for a reason; nothing is saved until you press Save (mail apps sometimes open
links on their own). POST /f/<job_id> saves and redirects back to the page.

Python's standard HTTP server, one request at a time: it's one person's LAN page. Every
request must carry the job's signature (links.py); anything else gets a 403 or 404.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape
from pydantic import SecretStr
from sqlalchemy.orm import Session, sessionmaker

from job_scout.config import AppConfig
from job_scout.db.groups import group_of
from job_scout.db.models import Vote
from job_scout.db.types import utcnow
from job_scout.feedback.links import verify
from job_scout.feedback.store import MAX_REASON_CHARS, current_score, get_vote, record_vote
from job_scout.report.render import http_url
from job_scout.scoring.schema import ScoreResult

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 16 * 1024
REQUEST_TIMEOUT_SECONDS = 15
_PATH = re.compile(r"/f/(\d{1,12})")
_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

_ENV = Environment(
    loader=PackageLoader("job_scout.feedback", "templates"),
    autoescape=select_autoescape(enabled_extensions=("html",), default_for_string=True),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)
_ENV.filters["http_url"] = http_url


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

    def handle(self, method: str, target: str, body: str = "") -> Response:
        url = urlsplit(target)
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
            score = current_score(session, job_id, self.config.profile.sha256)
            result = ScoreResult.model_validate(score.result) if score and score.result else None
            previous = get_vote(session, job_id)
            context: dict[str, Any] = {
                "title": f"Feedback: {job.title}",
                "error": None,
                "saved": saved,
                "job_id": job_id,
                "job_title": job.title,
                "company": job.company or "unknown company",
                "apply_url": job.apply_url or job.canonical_url,
                "score": result.overall_score if result else None,
                "disqualified": bool(result and result.disqualified),
                "summary": result.summary if result else None,
                "previous": previous,
                "vote": vote if vote in set(Vote) else (previous.vote if previous else ""),
                "reason": (previous.reason if previous else None) or "",
                "signature": signature,
                "max_reason": MAX_REASON_CHARS,
            }
            html = _ENV.get_template("page.html").render(context)
        return Response(200, html, {"Content-Type": "text/html; charset=utf-8"})

    def _save(self, job_id: int, signature: str, vote: Vote, reason: str) -> Response:
        with self.sf.begin() as session:
            group = group_of(session, job_id)
            if group is None or group.canonical.id != job_id:
                return self._error(404, "That job isn't in the database.")
            record_vote(session, job_id, vote, reason, self.config.profile.sha256, self.clock())
        log.info("feedback: job %s %s%s", job_id, vote.value, " with a reason" if reason else "")
        # Post/Redirect/Get: reloading the page doesn't resubmit the form.
        location = f"/f/{job_id}?" + urlencode({"s": signature, "saved": "1"})
        return Response(303, "", {"Location": location})

    def _error(self, status: int, message: str) -> Response:
        context = {"title": "job-scout feedback", "error": message}
        html = _ENV.get_template("page.html").render(context)
        return Response(status, html, {"Content-Type": "text/html; charset=utf-8"})


class _Handler(BaseHTTPRequestHandler):
    app: FeedbackApp  # set on the subclass `serve` builds
    timeout = REQUEST_TIMEOUT_SECONDS  # a stalled client can't hold the page forever
    server_version = "job-scout"
    sys_version = ""

    def do_GET(self) -> None:
        self._send(self.app.handle("GET", self.path))

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
        self.send_response(response.status)
        for name, value in {**_HEADERS, **response.headers}.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        # The default line has the full URL; keep the signature out of the logs.
        log.debug("%s %s %s", self.command, urlsplit(self.path).path, args[1] if args else "")


def make_server(app: FeedbackApp, host: str, port: int) -> HTTPServer:
    handler = type("FeedbackHandler", (_Handler,), {"app": app})
    return HTTPServer((host, port), handler)
