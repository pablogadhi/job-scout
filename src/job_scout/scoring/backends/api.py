"""`api` backend: the Anthropic Messages API (+ Message Batches), billed per token.

ANTHROPIC_API_KEY is read only here. Structured output via `output_config.format` with the
shared JSON schema; the reply is validated with Pydantic by the caller. The system prompt
is marked for prompt caching (Haiku 4.5 only caches prefixes of 4,096+ tokens, so it has no
effect there; Haiku 5.5 caches from 512). Haiku 5.5 thinks by default (adaptive, steered by
`effort`); thinking counts toward max_tokens, hence `thinking_headroom_tokens`.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator, Mapping
from typing import Any

import anthropic
from anthropic.types import OutputConfigParam, TextBlockParam
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming

from job_scout.config.secrets import secret
from job_scout.config.settings import ApiOptions, Effort, ModelPrices
from job_scout.db.models import Backend
from job_scout.scoring.backends.base import CallRequest, CallResult

# Above about 21k, the SDK refuses non-streaming requests (expected to exceed its timeout).
NON_STREAMING_MAX_TOKENS = 20_000


def estimate_cost(usage: Any, prices: ModelPrices | None, discount: float = 1.0) -> float | None:
    """USD for one response's usage; None when the model has no configured prices."""
    if prices is None or usage is None:
        return None
    per_token = 1 / 1_000_000
    total = (
        (usage.input_tokens or 0) * prices.input
        + (getattr(usage, "cache_creation_input_tokens", None) or 0) * prices.cache_write
        + (getattr(usage, "cache_read_input_tokens", None) or 0) * prices.cache_read
        + (usage.output_tokens or 0) * prices.output
    )
    return total * per_token * discount


def _input_tokens(usage: Any) -> int | None:
    if usage is None:
        return None
    return (
        (usage.input_tokens or 0)
        + (getattr(usage, "cache_creation_input_tokens", None) or 0)
        + (getattr(usage, "cache_read_input_tokens", None) or 0)
    )


def interpret(message: Any, prices: ModelPrices | None, discount: float = 1.0) -> CallResult:
    """Turn a Messages API response into a CallResult."""
    usage = message.usage
    common = {
        "input_tokens": _input_tokens(usage),
        "output_tokens": getattr(usage, "output_tokens", None),
        "cost_usd": estimate_cost(usage, prices, discount),
        "extra": {
            "message_id": getattr(message, "id", None),
            "stop_reason": message.stop_reason,
            "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", None),
        },
    }
    if message.stop_reason == "refusal":
        details = getattr(message, "stop_details", None)
        category = getattr(details, "category", None) if details else None
        return CallResult("refusal", error=f"refused (category: {category})", **common)
    if message.stop_reason == "max_tokens":
        return CallResult("truncated", error="output hit max_tokens", **common)
    text = next((b.text for b in message.content if b.type == "text"), None)
    try:
        payload = json.loads(text) if text else None
    except json.JSONDecodeError as exc:
        return CallResult("invalid", error=f"reply is not JSON: {exc}", **common)
    if payload is None:
        return CallResult("invalid", error="reply has no text block", **common)
    return CallResult("ok", payload=payload, **common)


class ApiScorer:
    backend = Backend.API

    def __init__(
        self,
        options: ApiOptions,
        model: str,
        client: anthropic.Anthropic | None = None,
        environ: Mapping[str, str] | None = None,
        effort: Effort | None = None,
    ) -> None:
        self.options = options
        self.model = model
        self.effort: Effort | None = effort  # annotated: pyright would widen it to str
        self.prices = options.prices.get(model)
        if client is None:
            key = secret(environ or {}, "ANTHROPIC_API_KEY")
            if key is None:
                raise RuntimeError("ANTHROPIC_API_KEY is not set (needed by the api backend)")
            client = anthropic.Anthropic(
                api_key=key.get_secret_value(), max_retries=options.max_retries
            )
        self.client: anthropic.Anthropic = client

    def params(self, request: CallRequest) -> MessageCreateParamsNonStreaming:
        system: TextBlockParam = {"type": "text", "text": request.system}
        if self.options.prompt_caching:
            system["cache_control"] = {"type": "ephemeral"}
        output_config: OutputConfigParam = {
            "format": {"type": "json_schema", "schema": request.schema}
        }
        if self.effort:
            output_config["effort"] = self.effort
        max_tokens = request.max_output_tokens + self.options.thinking_headroom_tokens
        return {
            "model": self.model,
            "max_tokens": min(max_tokens, NON_STREAMING_MAX_TOKENS),
            "system": [system],
            "messages": [{"role": "user", "content": request.user}],
            "output_config": output_config,
        }

    def score(self, request: CallRequest) -> CallResult:
        started = time.monotonic()
        try:
            message = self.client.messages.create(**self.params(request))
        except anthropic.APIStatusError as exc:
            return CallResult(
                "error", http_status=exc.status_code, error=f"{type(exc).__name__}: {exc.message}"
            )
        except anthropic.APIConnectionError as exc:  # includes timeouts, after SDK retries
            return CallResult("error", error=f"{type(exc).__name__}: {exc}")
        result = interpret(message, self.prices)
        result.duration_ms = int((time.monotonic() - started) * 1000)
        result.http_status = 200
        return result

    # --- Message Batches -----------------------------------------------------------------

    def submit_batch(self, requests: Mapping[str, CallRequest]) -> str:
        batch = self.client.messages.batches.create(
            requests=[
                {"custom_id": custom_id, "params": self.params(request)}
                for custom_id, request in requests.items()
            ]
        )
        return batch.id

    def batch_ended(self, batch_id: str) -> bool:
        return self.client.messages.batches.retrieve(batch_id).processing_status == "ended"

    def batch_results(self, batch_id: str) -> Iterator[tuple[str, CallResult]]:
        """(custom_id, result) for every request, in any order."""
        for item in self.client.messages.batches.results(batch_id):
            result = item.result
            if result.type == "succeeded":
                yield (
                    item.custom_id,
                    interpret(result.message, self.prices, self.options.batch_discount),
                )
            elif result.type == "errored":
                yield (
                    item.custom_id,
                    CallResult("error", error=f"batch request errored: {result.error}"),
                )
            else:  # canceled / expired: not the posting's fault, nothing was billed
                yield (
                    item.custom_id,
                    CallResult(
                        "error", error=f"batch request {result.type}", extra={"retry_free": True}
                    ),
                )
