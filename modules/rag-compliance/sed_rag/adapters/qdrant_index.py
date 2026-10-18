"""Індекс нормативного корпусу в Qdrant: dense (bge-m3, 1024, cosine) + sparse BM25 з IDF на сервері.

Одна колекція, два іменовані вектори в кожній точці:
* `dense` — ембединг «назва акта › розділ › пункт + текст пункту»;
* `bm25`  — sparse-вектор з `Modifier.IDF` (IDF рахує Qdrant за поточним корпусом).

Point id = `regulatory_chunks.id`, тож цитата в `compliance_citations` і точка в Qdrant —
той самий фрагмент. Чинність фільтрується на боці Qdrant за payload-індексами
(`eff_from`/`eff_to` як int YYYYMMDD, `status`, `is_current`), до ранжування.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date
from typing import Any

from qdrant_client import AsyncQdrantClient, models
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

from sed_db.enums import RegulatoryLevel, RegulatoryStatus

from ..corpus.sparse import SparseVector
from ..domain.models import (
    DATE_MAX,
    DATE_MIN,
    ActMeta,
    ChunkRecord,
    ConfigurationError,
    IndexUnavailable,
    date_key,
)
from ..ports import IndexedSource, ScoredPoint

DENSE = "dense"
SPARSE = "bm25"
_UPSERT_BATCH = 128


def act_payload(act: ActMeta) -> dict[str, Any]:
    return {
        "title": act.title, "short_title": act.short_title, "level": act.level.value, "issuer": act.issuer,
        "status": act.status.value, "doc_number": act.doc_number,
        "adopted_on": act.adopted_on.isoformat() if act.adopted_on else None,
        "effective_from": act.effective_from.isoformat() if act.effective_from else None,
        "effective_to": act.effective_to.isoformat() if act.effective_to else None,
        "source_url": act.source_url, "is_sample": act.is_sample,
    }


def act_from_payload(p: dict[str, Any]) -> ActMeta:
    def d(v: str | None) -> date | None:
        return date.fromisoformat(v) if v else None
    return ActMeta(
        title=p["title"], level=RegulatoryLevel(p["level"]), issuer=p["issuer"],
        status=RegulatoryStatus(p["status"]), doc_number=p.get("doc_number"),
        adopted_on=d(p.get("adopted_on")), effective_from=d(p.get("effective_from")),
        effective_to=d(p.get("effective_to")), short_title=p.get("short_title"),
        source_url=p.get("source_url"), is_sample=bool(p.get("is_sample")),
    )


def _status_fields(act: ActMeta) -> dict[str, Any]:
    return {
        "act": act_payload(act),
        "status": act.status.value,
        "level": act.level.value,
        "eff_from": date_key(act.effective_from, DATE_MIN),
        "eff_to": date_key(act.effective_to, DATE_MAX),
    }


class QdrantVectorIndex:
    """Реалізація порту `VectorIndex`."""

    def __init__(self, client: AsyncQdrantClient, collection: str) -> None:
        self.client = client
        self.collection = collection

    # --- схема ------------------------------------------------------------------------

    async def ensure_collection(self, dim: int) -> None:
        try:
            if await self.client.collection_exists(self.collection):
                info = await self.client.get_collection(self.collection)
                vectors = info.config.params.vectors
                size = vectors[DENSE].size if isinstance(vectors, dict) and DENSE in vectors else None
                if size != dim:
                    raise ConfigurationError(
                        f"Колекція Qdrant «{self.collection}» має розмірність {size}, а модель ембедингів — {dim}. "
                        "Створіть нову колекцію і переіндексуйте корпус.")
                return
            await self.client.create_collection(
                self.collection,
                vectors_config={DENSE: models.VectorParams(
                    size=dim, distance=models.Distance.COSINE,
                    hnsw_config=models.HnswConfigDiff(m=16, ef_construct=128))},
                sparse_vectors_config={SPARSE: models.SparseVectorParams(modifier=models.Modifier.IDF)},
            )
            for name, schema in (
                ("is_current", models.PayloadSchemaType.BOOL),
                ("source_id", models.PayloadSchemaType.KEYWORD),
                ("status", models.PayloadSchemaType.KEYWORD),
                ("level", models.PayloadSchemaType.KEYWORD),
                ("eff_from", models.PayloadSchemaType.INTEGER),
                ("eff_to", models.PayloadSchemaType.INTEGER),
                ("chunk_no", models.PayloadSchemaType.INTEGER),
                ("revision", models.PayloadSchemaType.INTEGER),
            ):
                await self.client.create_payload_index(self.collection, name, field_schema=schema)
        except (ResponseHandlingException, UnexpectedResponse, OSError) as exc:
            raise IndexUnavailable("Векторна БД (Qdrant) недоступна") from exc

    # --- запис ------------------------------------------------------------------------

    async def upsert(self, act: ActMeta, source_id: uuid.UUID, chunks: Sequence[ChunkRecord],
                     dense: Sequence[Sequence[float]], sparse: Sequence[SparseVector]) -> None:
        if not (len(chunks) == len(dense) == len(sparse)):
            raise ValueError("chunks, dense і sparse мають бути однакової довжини")
        points = [
            models.PointStruct(
                id=str(c.id),
                vector={DENSE: list(dv), SPARSE: models.SparseVector(indices=sv.indices, values=sv.values)},
                payload={
                    "chunk_id": str(c.id), "source_id": str(source_id), "revision": c.index_revision,
                    "chunk_no": c.chunk_no, "section_ref": c.section_ref, "heading_path": list(c.heading_path),
                    "content": c.content, "text_quality": c.text_quality, "is_current": True,
                    **_status_fields(act),
                },
            )
            for c, dv, sv in zip(chunks, dense, sparse, strict=True)
        ]
        await self._call(self._upsert_batches(points))

    async def _upsert_batches(self, points: list[models.PointStruct]) -> None:
        for i in range(0, len(points), _UPSERT_BATCH):
            await self.client.upsert(self.collection, points=points[i:i + _UPSERT_BATCH], wait=True)

    async def retire(self, source_id: uuid.UUID, keep_revision: int) -> None:
        await self._call(self.client.set_payload(
            self.collection, payload={"is_current": False}, wait=True,
            points=models.Filter(must=[
                models.FieldCondition(key="source_id", match=models.MatchValue(value=str(source_id))),
                models.FieldCondition(key="is_current", match=models.MatchValue(value=True)),
            ], must_not=[models.FieldCondition(key="revision", match=models.MatchValue(value=keep_revision))]),
        ))

    async def delete_points(self, ids: Sequence[uuid.UUID]) -> None:
        if ids:
            await self._call(self.client.delete(
                self.collection, points_selector=models.PointIdsList(points=[str(i) for i in ids]), wait=True))

    async def set_act_status(self, source_id: uuid.UUID, act: ActMeta) -> None:
        await self._call(self.client.set_payload(
            self.collection, payload=_status_fields(act), wait=True,
            points=models.Filter(must=[
                models.FieldCondition(key="source_id", match=models.MatchValue(value=str(source_id)))]),
        ))

    # --- пошук ------------------------------------------------------------------------

    @staticmethod
    def in_force_filter(as_of: date) -> models.Filter:
        k = date_key(as_of, DATE_MIN)
        return models.Filter(
            must=[
                models.FieldCondition(key="is_current", match=models.MatchValue(value=True)),
                models.FieldCondition(key="eff_from", range=models.Range(lte=k)),
                models.FieldCondition(key="eff_to", range=models.Range(gte=k)),
            ],
            must_not=[models.FieldCondition(key="status", match=models.MatchValue(value=RegulatoryStatus.DRAFT.value))],
        )

    async def search_dense(self, vector: Sequence[float], as_of: date, limit: int) -> list[ScoredPoint]:
        res = await self._call(self.client.query_points(
            self.collection, query=list(vector), using=DENSE, query_filter=self.in_force_filter(as_of),
            limit=limit, with_payload=True,
        ))
        return [ScoredPoint(uuid.UUID(str(p.id)), float(p.score), p.payload or {}) for p in res.points]

    async def search_sparse(self, vector: SparseVector, as_of: date, limit: int) -> list[ScoredPoint]:
        if not vector.indices:
            return []
        res = await self._call(self.client.query_points(
            self.collection, query=models.SparseVector(indices=vector.indices, values=vector.values),
            using=SPARSE, query_filter=self.in_force_filter(as_of), limit=limit, with_payload=True,
        ))
        return [ScoredPoint(uuid.UUID(str(p.id)), float(p.score), p.payload or {}) for p in res.points]

    async def catalog(self) -> list[IndexedSource]:
        flt = models.Filter(must=[
            models.FieldCondition(key="is_current", match=models.MatchValue(value=True)),
            models.FieldCondition(key="chunk_no", match=models.MatchValue(value=0)),
        ])
        out: list[IndexedSource] = []
        offset: Any = None
        while True:
            points, offset = await self._call(self.client.scroll(
                self.collection, scroll_filter=flt, limit=256, offset=offset, with_payload=True, with_vectors=False))
            for p in points:
                pl = p.payload or {}
                out.append(IndexedSource(
                    source_id=uuid.UUID(pl["source_id"]), meta=act_from_payload(pl["act"]),
                    first_chunk_id=uuid.UUID(str(p.id)), first_chunk_ref=pl["section_ref"],
                    first_chunk_text=pl["content"],
                ))
            if offset is None:
                return out

    async def count(self) -> int:
        res = await self._call(self.client.count(self.collection, exact=True))
        return int(res.count)

    async def _call(self, aw: Any) -> Any:
        try:
            return await aw
        except (ResponseHandlingException, UnexpectedResponse, OSError) as exc:
            raise IndexUnavailable("Векторна БД (Qdrant) недоступна") from exc
