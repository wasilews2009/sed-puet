"""Ембединги через OpenAI-сумісний `/v1/embeddings` (vLLM `--task embed`, Ollama ≥ 0.5, HF TEI)
і необов'язковий reranker HF TEI `/rerank` (напр. `BAAI/bge-reranker-v2-m3`).

Модель за замовчуванням — `BAAI/bge-m3` (1024 виміри, багатомовна, добре працює з українською).
Для `intfloat/multilingual-e5-large` задайте префікси «query: » / «passage: ».
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Sequence

import httpx

from ..domain.models import ConfigurationError, EmbeddingError

log = logging.getLogger(__name__)
_RETRYABLE = {408, 429, 500, 502, 503, 504}


async def _post(client: httpx.AsyncClient, path: str, body: dict, what: str,  # type: ignore[type-arg]
                retries: int = 2, backoff_s: float = 0.5) -> httpx.Response:
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = await client.post(path, json=body)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            last = exc
        else:
            if resp.status_code < 400:
                return resp
            if resp.status_code not in _RETRYABLE:
                raise EmbeddingError(f"Сервер {what} відхилив запит (HTTP {resp.status_code})")
            last = EmbeddingError(f"HTTP {resp.status_code}")
        if attempt < retries:
            await asyncio.sleep(backoff_s * 2**attempt)
    raise EmbeddingError(f"Сервер {what} недоступний") from last


def _l2(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class OpenAICompatibleEmbedder:
    """Реалізація порту `Embedder`."""

    def __init__(self, client: httpx.AsyncClient, model: str, version: str, dim: int, *,
                 query_prefix: str = "", passage_prefix: str = "", batch_size: int = 32) -> None:
        self.client = client
        self.model_name = model
        self.model_version = version
        self.dim = dim
        self.query_prefix = query_prefix
        self.passage_prefix = passage_prefix
        self.batch_size = batch_size

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            out.extend(await self._embed([self.passage_prefix + t for t in texts[i:i + self.batch_size]]))
        return out

    async def embed_query(self, text: str) -> list[float]:
        return (await self._embed([self.query_prefix + text]))[0]

    async def _embed(self, inputs: list[str]) -> list[list[float]]:
        resp = await _post(self.client, "/embeddings", {"model": self.model_name, "input": inputs}, "ембедингів")
        try:
            data = sorted(resp.json()["data"], key=lambda d: d["index"])
            vectors = [[float(x) for x in d["embedding"]] for d in data]
        except (ValueError, KeyError, TypeError) as exc:
            raise EmbeddingError("Сервер ембедингів повернув відповідь у неочікуваному форматі") from exc
        if len(vectors) != len(inputs):
            raise EmbeddingError("Сервер ембедингів повернув не всі вектори")
        for v in vectors:
            if len(v) != self.dim:
                raise ConfigurationError(
                    f"Модель ембедингів повертає {len(v)} вимірів, очікується {self.dim} "
                    "(regulatory_chunks.embedding / колекція Qdrant)")
        return [_l2(v) for v in vectors]


class TeiReranker:
    """Cross-encoder reranker через HF Text Embeddings Inference `/rerank`."""

    def __init__(self, client: httpx.AsyncClient, model_name: str = "BAAI/bge-reranker-v2-m3") -> None:
        self.client = client
        self.model_name = model_name

    async def rerank(self, query: str, texts: Sequence[str]) -> list[float]:
        if not texts:
            return []
        resp = await _post(self.client, "/rerank",
                           {"query": query, "texts": list(texts), "raw_scores": False, "truncate": True}, "reranker")
        try:
            scores = [0.0] * len(texts)
            for item in resp.json():
                scores[int(item["index"])] = float(item["score"])
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            raise EmbeddingError("Reranker повернув відповідь у неочікуваному форматі") from exc
        return scores
