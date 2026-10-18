"""Гібридний пошук норм: dense (bge-m3) + sparse (BM25) → Reciprocal Rank Fusion → reranker.

Обидва канали фільтруються в Qdrant за чинністю на дату перевірки, тож скасовані й ще не чинні
акти не потрапляють у кандидати (посилання на них перевіряє окремий вузол графа).
RRF рахується тут, а не в Qdrant (`FusionQuery`), щоб кожна цитата зберегла сліди обох
каналів (`dense_score`, `sparse_score`, `fused_score`, `rerank_score` у `compliance_citations`).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, replace
from datetime import date

from ..adapters.qdrant_index import act_from_payload
from ..config import RetrievalSettings
from ..corpus.sparse import Bm25Encoder
from ..domain.models import Coverage, RetrievedClause
from ..ports import Embedder, Reranker, ScoredPoint, VectorIndex


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    clauses: list[RetrievedClause]
    coverage: Coverage
    best_dense: float | None
    best_rerank: float | None


class HybridRetriever:
    def __init__(self, index: VectorIndex, embedder: Embedder, bm25: Bm25Encoder,
                 settings: RetrievalSettings, reranker: Reranker | None = None) -> None:
        self.index = index
        self.embedder = embedder
        self.bm25 = bm25
        self.s = settings
        self.reranker = reranker

    def config(self) -> dict[str, object]:
        return {
            "dense_k": self.s.dense_k, "sparse_k": self.s.sparse_k, "fusion": "rrf", "rrf_k": self.s.rrf_k,
            "final_k": self.s.final_k, "max_per_source": self.s.max_per_source,
            "reranker": self.reranker.model_name if self.reranker else None, "rerank_top_n": self.s.rerank_top_n,
            "embedding_model": f"{self.embedder.model_name}:{self.embedder.model_version}",
            "bm25": self.bm25.config(), "min_dense_score": self.s.min_dense_score,
        }

    async def retrieve(self, query: str, as_of: date, *, k: int | None = None) -> RetrievalResult:
        k = k or self.s.final_k
        dense_vec = await self.embedder.embed_query(query)
        dense, sparse = await asyncio.gather(
            self.index.search_dense(dense_vec, as_of, self.s.dense_k),
            self.index.search_sparse(self.bm25.encode_query(query), as_of, self.s.sparse_k),
        )
        fused = self._fuse(dense, sparse)
        if not fused:
            return RetrievalResult([], Coverage.NONE, None, None)

        if self.reranker is not None:
            head = fused[: self.s.rerank_top_n]
            scores = await self.reranker.rerank(query, [c.content for c in head])
            head = [_with_rerank(c, s) for c, s in zip(head, scores, strict=True)]
            head.sort(key=lambda c: c.rerank_score or 0.0, reverse=True)
            fused = head + fused[self.s.rerank_top_n:]

        picked: list[RetrievedClause] = []
        per_source: dict[uuid.UUID, int] = {}
        for c in fused:
            if per_source.get(c.source_id, 0) >= self.s.max_per_source:
                continue
            per_source[c.source_id] = per_source.get(c.source_id, 0) + 1
            picked.append(c)
            if len(picked) >= k:
                break

        best_dense = max((p.score for p in dense), default=None)
        best_rerank = max((c.rerank_score for c in picked if c.rerank_score is not None), default=None)
        return RetrievalResult(picked, self._coverage(best_dense, best_rerank, bool(sparse)), best_dense, best_rerank)

    def _fuse(self, dense: list[ScoredPoint], sparse: list[ScoredPoint]) -> list[RetrievedClause]:
        rows: dict[uuid.UUID, dict] = {}  # type: ignore[type-arg]
        for channel, hits in (("dense", dense), ("sparse", sparse)):
            for rank, p in enumerate(hits, start=1):
                r = rows.setdefault(p.chunk_id, {"payload": p.payload, "fused": 0.0})
                r[f"{channel}_score"] = p.score
                r[f"{channel}_rank"] = rank
                r["fused"] += 1.0 / (self.s.rrf_k + rank)
        top = 2.0 / (self.s.rrf_k + 1)          # максимум: перше місце в обох каналах
        out = [
            RetrievedClause(
                chunk_id=cid, source_id=uuid.UUID(r["payload"]["source_id"]),
                act=act_from_payload(r["payload"]["act"]), section_ref=r["payload"]["section_ref"],
                heading_path=tuple(r["payload"].get("heading_path") or ()), content=r["payload"]["content"],
                text_quality=float(r["payload"].get("text_quality", 1.0)),
                dense_score=r.get("dense_score"), sparse_score=r.get("sparse_score"),
                fused_score=round(r["fused"] / top, 6), dense_rank=r.get("dense_rank"),
                sparse_rank=r.get("sparse_rank"),
            )
            for cid, r in rows.items()
        ]
        out.sort(key=lambda c: (-c.fused_score, c.dense_rank or 10**6))
        return out

    def _coverage(self, best_dense: float | None, best_rerank: float | None, has_sparse: bool) -> Coverage:
        if best_rerank is not None:
            return Coverage.STRONG if best_rerank >= self.s.min_rerank_score else Coverage.WEAK
        if best_dense is not None and best_dense >= self.s.min_dense_score:
            return Coverage.STRONG
        return Coverage.WEAK if (best_dense is not None or has_sparse) else Coverage.NONE


def _with_rerank(c: RetrievedClause, score: float) -> RetrievedClause:
    return replace(c, rerank_score=score)
