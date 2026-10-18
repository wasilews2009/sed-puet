"""HTTP-адаптери: OpenAI-сумісні ембединги (vLLM/Ollama/TEI) і TEI reranker на httpx.MockTransport."""

from __future__ import annotations

import json

import httpx
import pytest

from sed_rag.adapters.embeddings import OpenAICompatibleEmbedder, TeiReranker
from sed_rag.domain.models import ConfigurationError, EmbeddingError


def _client(handler) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://emb/v1")


async def test_embeddings_batching_order_prefix_and_normalization() -> None:
    seen: list[dict] = []  # type: ignore[type-arg]

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen.append(body)
        data = [{"index": i, "embedding": [float(i + 1)] + [0.0] * 3} for i in range(len(body["input"]))]
        return httpx.Response(200, json={"data": list(reversed(data))})       # сервер може змішати порядок

    async with _client(handler) as c:
        e = OpenAICompatibleEmbedder(c, "bge-m3", "1", 4, passage_prefix="passage: ", query_prefix="query: ", batch_size=2)
        vecs = await e.embed_documents(["a", "b", "c"])
        q = await e.embed_query("q")
    assert len(seen) == 3 and seen[0]["input"] == ["passage: a", "passage: b"] and seen[2]["input"] == ["query: q"]
    assert [v[0] for v in vecs] == [1.0, 1.0, 1.0] and q == [1.0, 0.0, 0.0, 0.0]   # L2-нормовано


async def test_embeddings_dimension_mismatch() -> None:
    async with _client(lambda r: httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1] * 768}]})) as c:
        with pytest.raises(ConfigurationError, match="768"):
            await OpenAICompatibleEmbedder(c, "e5", "1", 1024).embed_query("x")


async def test_embeddings_retry_then_fail() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] < 2 else httpx.Response(
            200, json={"data": [{"index": 0, "embedding": [1.0, 0.0]}]})

    async with _client(handler) as c:
        e = OpenAICompatibleEmbedder(c, "m", "1", 2)
        assert await e.embed_query("x") == [1.0, 0.0]
    async with _client(lambda r: httpx.Response(400)) as c:
        with pytest.raises(EmbeddingError, match="HTTP 400"):
            await OpenAICompatibleEmbedder(c, "m", "1", 2).embed_query("x")


async def test_tei_reranker() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path.endswith("/rerank")
        return httpx.Response(200, json=[{"index": 1, "score": 0.9}, {"index": 0, "score": 0.2}])

    async with _client(handler) as c:
        assert await TeiReranker(c).rerank("q", ["a", "b"]) == [0.2, 0.9]
        assert await TeiReranker(c).rerank("q", []) == []
