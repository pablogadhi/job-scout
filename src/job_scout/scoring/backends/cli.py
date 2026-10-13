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

from job_scout.config.settings import CliOptions
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
        effort: str | None = None,
    ) -> None:
        self.options = options
        self.model = model
        self.effort = effort
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


def parse_output(returncode: int, stdout: str, stderr: str, duration_ms: int) -> CallResult:
    data: dict[str, Any] | None = None
    try:
        parsed = json.loads(stdout) if stdout.strip() else None
        data = parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        data = None
    text = " ".join(
        filter(
            None, [str((data or {}).get("result") or ""), stdout if data is None else "", stderr]
        )
    )

    usage = (data or {}).get("usage") or {}
    common = {
        "input_tokens": _tokens(
            usage, "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"
        ),
        "output_tokens": usage.get("output_tokens"),
        "cost_usd": (data or {}).get("total_cost_usd"),
        "duration_ms": duration_ms,
        "exit_code": returncode,
        "extra": {
            "session_id": (data or {}).get("session_id"),
            "subtype": (data or {}).get("subtype"),
            "stop_reason": (data or {}).get("stop_reason"),
            "num_turns": (data or {}).get("num_turns"),
            # Per-model token usage: shows whether the CLI also ran other models.
            "model_usage": (data or {}).get("modelUsage"),
        },
    }
    if _USAGE_LIMIT.search(text):
        return CallResult("usage_limit", error=_short(text), **common)
    if returncode != 0 or data is None or data.get("is_error"):
        # `subtype` names the failure (e.g. error_max_turns) when `result` is empty.
        subtype = (data or {}).get("subtype")
        reason = " ".join(filter(None, [subtype if subtype != "success" else None, text]))
        error = _short(reason, 1_500) or f"exit code {returncode}"
        return CallResult("error", error=f"{error} (exit code {returncode})", **common)
    if data.get("stop_reason") == "refusal":
        return CallResult("refusal", error=_short(text), **common)
    if data.get("stop_reason") == "max_tokens":
        return CallResult("truncated", error="output hit the token limit", **common)
    output = data.get("structured_output")
    if output is None:
        return CallResult("invalid", error="no structured_output in CLI response", **common)
    return CallResult("ok", payload=output, **common)


def _tokens(usage: Mapping[str, Any], *keys: str) -> int | None:
    values = [int(v) for k in keys if (v := usage.get(k)) is not None]
    return sum(values) if values else None


def _short(text: str, limit: int = 500) -> str:
    return " ".join(text.split())[:limit]
