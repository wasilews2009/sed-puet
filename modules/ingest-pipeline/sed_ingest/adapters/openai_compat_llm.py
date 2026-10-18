"""Клієнт локальної LLM через OpenAI-сумісний `/v1/chat/completions` (vLLM, Ollama ≥ 0.5, llama.cpp server).

Structured output: `response_format = {type: json_schema, json_schema: {schema, strict: true}}` —
vLLM застосовує guided decoding (xgrammar/outlines), Ollama — граматику з JSON Schema,
тож модель фізично не може згенерувати JSON поза схемою. Pydantic-валідація після цього
однаково обов'язкова: схема не перевіряє бізнес-обмеження, а сервер може бути без guided decoding.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

import httpx

from ..domain.models import LlmCompletion, LlmError, LlmIdentity

log = logging.getLogger(__name__)

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)
_RETRYABLE = {408, 429, 500, 502, 503, 504}


class OpenAICompatibleLlm:
    """Реалізація порту `StructuredLlm`."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        model: str,
        identity: LlmIdentity,
        *,
        max_tokens: int = 2048,
        seed: int | None = 42,
        transport_retries: int = 2,
        backoff_s: float = 1.0,
    ) -> None:
        self.client = client          # base_url = …/v1, таймаути й заголовки задає фабрика
        self.model = model
        self.identity = identity
        self.max_tokens = max_tokens
        self.seed = seed
        self.transport_retries = transport_retries
        self.backoff_s = backoff_s

    async def complete_json(
        self, messages: list[dict[str, str]], schema: dict[str, Any], schema_name: str
    ) -> LlmCompletion:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": self.max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": schema, "strict": True},
            },
        }
        if self.seed is not None:
            body["seed"] = self.seed

        started = time.monotonic()
        resp = await self._post_with_retry(body)
        latency = int((time.monotonic() - started) * 1000)

        try:
            payload = resp.json()
            choice = payload["choices"][0]
            content = choice["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LlmError("Сервер LLM повернув відповідь у неочікуваному форматі") from exc
        if choice.get("finish_reason") == "length":
            raise LlmError("Відповідь LLM обрізано лімітом токенів — збільште llm_max_output_tokens")
        if not isinstance(content, str) or not content.strip():
            raise LlmError("LLM повернула порожню відповідь")

        parsed = _parse_json(content)
        usage = payload.get("usage") or {}
        return LlmCompletion(
            content=parsed,
            model=payload.get("model", self.model),
            tokens_in=usage.get("prompt_tokens"),
            tokens_out=usage.get("completion_tokens"),
            latency_ms=latency,
            attempts=1,
        )

    async def _post_with_retry(self, body: dict[str, Any]) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(self.transport_retries + 1):
            try:
                resp = await self.client.post("/chat/completions", json=body)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last = exc
                log.warning("LLM transport error (attempt %s): %s", attempt + 1, type(exc).__name__)
            else:
                if resp.status_code < 400:
                    return resp
                if resp.status_code not in _RETRYABLE:
                    # 400 часто означає, що сервер не підтримує json_schema (стара версія Ollama/vLLM).
                    raise LlmError(f"Сервер LLM відхилив запит (HTTP {resp.status_code})")
                last = LlmError(f"HTTP {resp.status_code}")
            if attempt < self.transport_retries:
                await asyncio.sleep(self.backoff_s * 2**attempt)
        raise LlmError("Локальна LLM недоступна") from last


def _parse_json(content: str) -> dict[str, Any]:
    m = _FENCE.match(content)
    text = m.group(1) if m else content
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LlmError("LLM повернула невалідний JSON") from exc
    if not isinstance(value, dict):
        raise LlmError("LLM повернула JSON, що не є об'єктом")
    return value
