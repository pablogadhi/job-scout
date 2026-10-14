"""`cli` backend: the `claude` CLI in headless mode, billed to the Claude Pro subscription.

Isolation: runs in an empty working directory with every tool disabled, no MCP servers,
no user/project settings, no session persistence. Auth comes only from
CLAUDE_CODE_OAUTH_TOKEN; the subprocess environment is an allowlist, so ANTHROPIC_API_KEY
(which `-p` would prefer, billing the API instead of Pro) can never reach it. Never use
`--bare`: it ignores subscription logins.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from job_scout.config.settings import CliOptions, Effort
from job_scout.db.models import Backend
from job_scout.scoring.backends.base import CallRequest, CallResult

# Variables the CLI needs to run and authenticate; everything else is dropped.
ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TZ",
    "TMPDIR",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
)

_USAGE_LIMIT = re.compile(
    r"usage limit|hit your (usage )?limit|limit reached|limit will reset|resets at"
    r"|out of (extra )?usage",
    re.IGNORECASE,
)

Runner = Callable[..., subprocess.CompletedProcess[str]]


# Set for every call: no auto-update (the CLI version is pinned and updated deliberately,
# PLAN.md Phase 6), no telemetry or error reporting.
FIXED_ENV = {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}


def subprocess_env(environ: Mapping[str, str]) -> dict[str, str]:
    """Only allowlisted variables: ANTHROPIC_API_KEY and friends can never get through."""
    return {**{k: environ[k] for k in ENV_ALLOWLIST if k in environ}, **FIXED_ENV}


class CliScorer:
    backend = Backend.CLI

    def __init__(
        self,
        options: CliOptions,
        model: str,
        environ: Mapping[str, str] | None = None,
        run: Runner = subprocess.run,
        effort: Effort | None = None,
    ) -> None:
        self.options = options
        self.model = model
        self.effort: Effort | None = effort  # annotated: pyright would widen it to str
        self.env = subprocess_env(os.environ if environ is None else environ)
        self.run = run

    def command(self, request: CallRequest) -> list[str]:
        effort = ["--effort", self.effort] if self.effort else []
        return [
            self.options.binary,
            "-p",
            "--model", self.model,
            "--output-format", "json",
            "--json-schema", json.dumps(request.schema, separators=(",", ":")),
            "--system-prompt", request.system,
            "--tools", "",
            "--permission-mode", "dontAsk",
            "--strict-mcp-config",
            "--setting-sources", "",
            "--disable-slash-commands",
            "--no-session-persistence",
            *effort,
        ]  # fmt: skip

    def score(self, request: CallRequest) -> CallResult:
        workdir = Path(self.options.workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        try:
            proc = self.run(
                self.command(request),
                input=request.user,
                capture_output=True,
                text=True,
                cwd=workdir,
                env=self.env,
                timeout=self.options.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return CallResult(
                "timeout", error=f"claude CLI timed out after {self.options.timeout_seconds}s"
            )
        except OSError as exc:
            return CallResult("error", error=f"could not run {self.options.binary}: {exc}")
        duration_ms = int((time.monotonic() - started) * 1000)
        return parse_output(proc.returncode, proc.stdout, proc.stderr, duration_ms)


class _Usage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    output_tokens: int | None = None

    @property
    def total_input(self) -> int | None:
        parts = (self.input_tokens, self.cache_creation_input_tokens, self.cache_read_input_tokens)
        values = [v for v in parts if v is not None]
        return sum(values) if values else None


class _CliReply(BaseModel):
    """The `--output-format json` reply. Every field is optional and unknown ones are
    ignored: the CLI adds fields between releases."""

    model_config = ConfigDict(extra="ignore")

    result: str | None = None
    is_error: bool = False
    subtype: str | None = None
    stop_reason: str | None = None
    session_id: str | None = None
    num_turns: int | None = None
    total_cost_usd: float | None = None
    structured_output: Any = None
    usage: _Usage | None = None
    model_usage: dict[str, Any] | None = Field(default=None, alias="modelUsage")


def parse_output(returncode: int, stdout: str, stderr: str, duration_ms: int) -> CallResult:
    reply: _CliReply | None = None
    invalid: str | None = None
    try:
        parsed = json.loads(stdout) if stdout.strip() else None
        if isinstance(parsed, dict):
            reply = _CliReply.model_validate(parsed)
    except json.JSONDecodeError:
        pass
    except ValidationError as exc:
        invalid = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
    text = " ".join(
        filter(
            None, [(reply.result or "") if reply else "", stdout if reply is None else "", stderr]
        )
    )

    usage = reply.usage if reply and reply.usage else _Usage()
    common: dict[str, Any] = {
        "input_tokens": usage.total_input,
        "output_tokens": usage.output_tokens,
        "cost_usd": reply.total_cost_usd if reply else None,
        "duration_ms": duration_ms,
        "exit_code": returncode,
        "extra": {
            "session_id": reply.session_id if reply else None,
            "subtype": reply.subtype if reply else None,
            "stop_reason": reply.stop_reason if reply else None,
            "num_turns": reply.num_turns if reply else None,
            # Per-model token usage: shows whether the CLI also ran other models.
            "model_usage": reply.model_usage if reply else None,
        },
    }
    if _USAGE_LIMIT.search(text):
        return CallResult("usage_limit", error=_short(text), **common)
    if invalid and returncode == 0:
        return CallResult("invalid", error=_short(f"unexpected CLI reply: {invalid}"), **common)
    if returncode != 0 or reply is None or reply.is_error:
        # `subtype` names the failure (e.g. error_max_turns) when `result` is empty.
        subtype = reply.subtype if reply else None
        reason = " ".join(filter(None, [subtype if subtype != "success" else None, text]))
        error = _short(reason, 1_500) or f"exit code {returncode}"
        return CallResult("error", error=f"{error} (exit code {returncode})", **common)
    if reply.stop_reason == "refusal":
        return CallResult("refusal", error=_short(text), **common)
    if reply.stop_reason == "max_tokens":
        return CallResult("truncated", error="output hit the token limit", **common)
    if reply.structured_output is None:
        return CallResult("invalid", error="no structured_output in CLI response", **common)
    return CallResult("ok", payload=reply.structured_output, **common)


def _short(text: str, limit: int = 500) -> str:
    return " ".join(text.split())[:limit]
