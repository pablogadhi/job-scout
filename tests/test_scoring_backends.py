"""Both scoring backends in isolation: the CLI subprocess and the SDK client are faked."""

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest

from job_scout.config import AppConfig
from job_scout.config.settings import ModelPrices
from job_scout.scoring.backends.api import (
    NON_STREAMING_MAX_TOKENS,
    ApiScorer,
    estimate_cost,
    interpret,
)
from job_scout.scoring.backends.base import CallRequest
from job_scout.scoring.backends.cli import CliScorer, parse_output, subprocess_env
from job_scout.scoring.schema import output_json_schema
from tests.scoring_helpers import result_for

REQUEST = CallRequest(
    system="SYSTEM PROMPT",
    user="Score the following 1 job posting.",
    job_ids=(7,),
    schema=output_json_schema(),
    max_output_tokens=1_900,
)
HAIKU = ModelPrices(input=1.0, output=5.0, cache_write=1.25, cache_read=0.1)


# --- cli ---------------------------------------------------------------------------------


def cli_json(**fields: Any) -> str:
    base = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "",
        "structured_output": {"results": [result_for(7)]},
        "total_cost_usd": 0.0123,
        "usage": {"input_tokens": 3000, "cache_read_input_tokens": 500, "output_tokens": 450},
        "session_id": "s-1",
        "num_turns": 1,
        "modelUsage": {"claude-haiku-5-5": {"inputTokens": 3000}},
    }
    return json.dumps({**base, **fields})


def test_env_is_an_allowlist() -> None:
    env = subprocess_env(
        {
            "PATH": "/usr/bin",
            "HOME": "/home/x",
            "CLAUDE_CODE_OAUTH_TOKEN": "oauth",
            "ANTHROPIC_API_KEY": "sk-secret",
            "ANTHROPIC_AUTH_TOKEN": "t",
            "AWS_SECRET_ACCESS_KEY": "aws",
        }
    )
    assert env == {
        "PATH": "/usr/bin",
        "HOME": "/home/x",
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }


def test_cli_invocation(app_config: AppConfig, tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.update(cmd=cmd, **kwargs)
        return subprocess.CompletedProcess(cmd, 0, cli_json(), "")

    options = app_config.settings.scoring.cli.model_copy(update={"workdir": tmp_path / "cwd"})
    scorer = CliScorer(
        options, "claude-haiku-4-5", environ={"ANTHROPIC_API_KEY": "sk", "PATH": "/bin"}, run=run
    )
    result = scorer.score(REQUEST)

    assert result.status == "ok" and result.payload["results"][0]["job_id"] == 7
    assert (result.input_tokens, result.output_tokens, result.cost_usd) == (3500, 450, 0.0123)
    assert result.extra["num_turns"] == 1
    assert result.extra["model_usage"] == {"claude-haiku-5-5": {"inputTokens": 3000}}
    cmd = seen["cmd"]
    flag = lambda name: cmd[cmd.index(name) + 1]  # noqa: E731
    assert cmd[:2] == ["claude", "-p"] and "--bare" not in cmd
    assert flag("--model") == "claude-haiku-4-5"
    assert flag("--output-format") == "json"
    assert flag("--tools") == ""  # no tools at all
    assert flag("--system-prompt") == "SYSTEM PROMPT"
    assert json.loads(flag("--json-schema")) == REQUEST.schema
    assert {"--strict-mcp-config", "--no-session-persistence", "--disable-slash-commands"} <= set(
        cmd
    )
    assert seen["input"] == REQUEST.user  # postings on stdin
    assert seen["cwd"] == tmp_path / "cwd" and seen["cwd"].is_dir()
    assert seen["env"] == {"PATH": "/bin", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    assert seen["timeout"] == options.timeout_seconds


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "status"),
    [
        (0, cli_json(), "", "ok"),
        (
            1,
            cli_json(is_error=True, result="Claude AI usage limit reached|1791900000"),
            "",
            "usage_limit",
        ),
        (
            0,
            cli_json(is_error=True, result="You've hit your limit · resets 3pm"),
            "",
            "usage_limit",
        ),
        (1, "", "Error: invalid model claude-haiku-9", "error"),
        (0, cli_json(is_error=True, result="something broke"), "", "error"),
        (0, cli_json(structured_output=None), "", "invalid"),
        (0, cli_json(stop_reason="max_tokens"), "", "truncated"),
        (0, cli_json(stop_reason="refusal"), "", "refusal"),
        (0, "not json", "", "error"),
    ],
)
def test_parse_cli_output(returncode: int, stdout: str, stderr: str, status: str) -> None:
    assert parse_output(returncode, stdout, stderr, 10).status == status


def test_cli_error_names_the_subtype() -> None:
    stdout = cli_json(is_error=True, subtype="error_max_turns", result="")
    result = parse_output(1, stdout, "", 10)
    assert result.status == "error"
    assert result.error == "error_max_turns (exit code 1)"
    assert result.input_tokens == 3500  # usage is kept for failed calls too


def test_cli_timeout(app_config: AppConfig, tmp_path: Path) -> None:
    def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    options = app_config.settings.scoring.cli.model_copy(update={"workdir": tmp_path})
    result = CliScorer(options, "m", environ={}, run=run).score(REQUEST)
    assert result.status == "timeout" and "timed out" in result.error


# --- api ---------------------------------------------------------------------------------


def message(
    stop_reason: str = "end_turn", text: str | None = None, **usage: int
) -> SimpleNamespace:
    text = json.dumps({"results": [result_for(7)]}) if text is None else text
    return SimpleNamespace(
        id="msg_1",
        stop_reason=stop_reason,
        stop_details=SimpleNamespace(category="cyber") if stop_reason == "refusal" else None,
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=usage.get("input_tokens", 3000),
            output_tokens=usage.get("output_tokens", 450),
            cache_creation_input_tokens=usage.get("cache_creation_input_tokens", 0),
            cache_read_input_tokens=usage.get("cache_read_input_tokens", 0),
        ),
    )


class FakeMessages:
    def __init__(self, reply: Any) -> None:
        self.reply = reply
        self.calls: list[dict[str, Any]] = []
        self.batches = FakeBatches()

    def create(self, **params: Any) -> Any:
        self.calls.append(params)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class FakeBatches:
    def __init__(self) -> None:
        self.created: list[Any] = []
        self.polls = 0
        self.items: list[Any] = []

    def create(self, requests: list[dict[str, Any]]) -> Any:
        self.created.append(requests)
        return SimpleNamespace(id="msgbatch_1")

    def retrieve(self, batch_id: str) -> Any:
        self.polls += 1
        return SimpleNamespace(processing_status="ended" if self.polls >= 2 else "in_progress")

    def results(self, batch_id: str) -> Any:
        return iter(self.items)


def api_scorer(app_config: AppConfig, reply: Any) -> tuple[ApiScorer, FakeMessages]:
    messages = FakeMessages(reply)
    client = SimpleNamespace(messages=messages)
    return ApiScorer(app_config.settings.scoring.api, "claude-haiku-4-5", client=client), messages  # type: ignore[arg-type]


def test_api_request_shape(app_config: AppConfig) -> None:
    scorer, messages = api_scorer(app_config, message())
    result = scorer.score(REQUEST)
    assert result.status == "ok" and result.http_status == 200
    params = messages.calls[0]
    assert params["model"] == "claude-haiku-4-5"
    assert params["max_tokens"] == 1_900 + 8_000  # + thinking_headroom_tokens
    assert params["system"] == [
        {"type": "text", "text": "SYSTEM PROMPT", "cache_control": {"type": "ephemeral"}}
    ]
    assert params["messages"] == [{"role": "user", "content": REQUEST.user}]
    assert params["output_config"] == {"format": {"type": "json_schema", "schema": REQUEST.schema}}
    assert "temperature" not in params and "thinking" not in params  # Haiku 5.5 rejects these
    assert result.cost_usd == pytest.approx((3000 * 1 + 450 * 5) / 1e6)


def test_api_effort_and_max_tokens_cap(app_config: AppConfig) -> None:
    messages = FakeMessages(message())
    scorer = ApiScorer(
        app_config.settings.scoring.api,
        "claude-haiku-5-5",
        client=SimpleNamespace(messages=messages),  # type: ignore[arg-type]
        effort="low",
    )
    scorer.score(replace(REQUEST, max_output_tokens=16_000))
    params = messages.calls[0]
    assert params["output_config"]["effort"] == "low"
    assert params["max_tokens"] == NON_STREAMING_MAX_TOKENS


def test_cli_effort_flag(app_config: AppConfig, tmp_path: Path) -> None:
    options = app_config.settings.scoring.cli.model_copy(update={"workdir": tmp_path})
    assert "--effort" not in CliScorer(options, "m", environ={}).command(REQUEST)
    cmd = CliScorer(options, "m", environ={}, effort="low").command(REQUEST)
    assert cmd[cmd.index("--effort") + 1] == "low"


def test_api_key_required(app_config: AppConfig) -> None:
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        ApiScorer(app_config.settings.scoring.api, "claude-haiku-4-5", environ={})


def test_cost_with_cache_and_batch_discount() -> None:
    usage = SimpleNamespace(
        input_tokens=100,
        output_tokens=400,
        cache_creation_input_tokens=4_000,
        cache_read_input_tokens=8_000,
    )
    full = (100 * 1 + 4_000 * 1.25 + 8_000 * 0.1 + 400 * 5) / 1e6
    assert estimate_cost(usage, HAIKU) == pytest.approx(full)
    assert estimate_cost(usage, HAIKU, discount=0.5) == pytest.approx(full / 2)
    assert estimate_cost(usage, None) is None


@pytest.mark.parametrize(
    ("reply", "status"),
    [
        (message("refusal"), "refusal"),
        (message("max_tokens"), "truncated"),
        (message(text="{not json"), "invalid"),
    ],
)
def test_api_bad_replies(reply: Any, status: str) -> None:
    assert interpret(reply, HAIKU).status == status


def test_api_http_errors(app_config: AppConfig) -> None:
    response = httpx2.Response(
        429, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    scorer, _ = api_scorer(
        app_config, anthropic.RateLimitError("rate limited", response=response, body=None)
    )
    result = scorer.score(REQUEST)
    assert (result.status, result.http_status) == ("error", 429)
    assert "RateLimitError" in result.error


def test_batch_submit_poll_results(app_config: AppConfig) -> None:
    scorer, messages = api_scorer(app_config, None)
    batch_id = scorer.submit_batch({"c0": REQUEST})
    assert batch_id == "msgbatch_1"
    assert messages.batches.created[0][0]["custom_id"] == "c0"
    assert (
        messages.batches.created[0][0]["params"]["output_config"]["format"]["type"] == "json_schema"
    )
    assert not scorer.batch_ended(batch_id) and scorer.batch_ended(batch_id)
    messages.batches.items = [
        SimpleNamespace(
            custom_id="c0", result=SimpleNamespace(type="succeeded", message=message())
        ),
        SimpleNamespace(custom_id="c1", result=SimpleNamespace(type="errored", error="overloaded")),
        SimpleNamespace(custom_id="c2", result=SimpleNamespace(type="expired")),
    ]
    results = dict(scorer.batch_results(batch_id))
    assert results["c0"].status == "ok"
    assert results["c0"].cost_usd == pytest.approx((3000 + 450 * 5) / 1e6 * 0.5)  # batch price
    assert results["c1"].status == "error"
    assert results["c2"].extra == {"retry_free": True}
