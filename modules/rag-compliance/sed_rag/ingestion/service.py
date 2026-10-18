"""Індексація нормативного акта: текст → пункти → ембединги + BM25 → PostgreSQL і Qdrant.

Порядок гарантує, що цитата ніколи не посилається на фрагмент, якого немає в БД:

1. текст очищується і сегментується; SHA-256 нормалізованого тексту — ознака змін;
2. той самий текст уже проіндексовано → нічого не робиться (ідемпотентно);
3. ембединги рахуються ДО відкриття транзакції (повільний зовнішній виклик);
4. у транзакції PostgreSQL: нова ревізія акта і фрагментів (старі `is_current = false`, але
   не видаляються — на них посилаються цитати в історичних перевірках);
5. всередині тієї ж транзакції — upsert точок у Qdrant; збій Qdrant → ROLLBACK у БД;
6. після COMMIT — старі ревізії в Qdrant позначаються неактуальними; якщо COMMIT не вдався —
   щойно додані точки видаляються (компенсація).
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from enum import StrEnum

from ..corpus.segmentation import clean_text, segment
from ..corpus.sparse import Bm25Encoder, tokenize
from ..domain.models import ActMeta, ChunkRecord, InvalidSource, SourceRecord
from ..ports import CorpusRepository, Embedder, VectorIndex

log = logging.getLogger(__name__)


class IngestStatus(StrEnum):
    CREATED = "created"
    REINDEXED = "reindexed"
    UNCHANGED = "unchanged"


@dataclass(frozen=True, slots=True)
class IngestReport:
    status: IngestStatus
    source_id: uuid.UUID
    index_revision: int
    chunks: int
    sections: list[str]


class CorpusIngestor:
    def __init__(self, *, repository: CorpusRepository, index: VectorIndex, embedder: Embedder,
                 bm25: Bm25Encoder, max_chunk_chars: int = 1500) -> None:
        self.repo = repository
        self.index = index
        self.embedder = embedder
        self.bm25 = bm25
        self.max_chunk_chars = max_chunk_chars

    async def ingest(self, act: ActMeta, raw_text: str, *, actor_user_id: uuid.UUID,
                     text_quality: float = 1.0, force: bool = False) -> IngestReport:
        text = clean_text(raw_text)
        clauses = segment(text, max_chars=self.max_chunk_chars)
        if not clauses:
            raise InvalidSource(f"У тексті акта «{act.title}» не знайдено жодного пункту")
        sha = hashlib.sha256(text.encode()).hexdigest()

        existing = await self.repo.find_source(act)
        if existing and existing.content_sha256 == sha and not force:
            if existing.meta != act:
                await self.set_status(existing.id, act, actor_user_id=actor_user_id)
            return IngestReport(IngestStatus.UNCHANGED, existing.id, existing.index_revision, len(clauses), [])

        source_id = existing.id if existing else uuid.uuid4()
        revision = (existing.index_revision + 1) if existing else 1
        chunks = [
            ChunkRecord(
                id=uuid.uuid4(), source_id=source_id, index_revision=revision, chunk_no=i,
                section_ref=c.section_ref, heading_path=c.heading_path, content=c.text,
                token_count=len(tokenize(c.text)), text_quality=round(text_quality, 4),
            )
            for i, c in enumerate(clauses)
        ]
        embed_texts = [c.embedding_text(act) for c in chunks]
        dense = await self.embedder.embed_documents(embed_texts)
        sparse = [self.bm25.encode_document(t) for t in embed_texts]

        await self.index.ensure_collection(self.embedder.dim)
        source = SourceRecord(source_id, act, revision, sha)
        upserted = False
        try:
            async with self.repo.save_revision(source, chunks, actor_user_id):
                await self.index.upsert(act, source_id, chunks, dense, sparse)
                upserted = True
        except BaseException:
            if upserted:
                log.warning("DB commit failed after Qdrant upsert; deleting %d points", len(chunks))
                await self.index.delete_points([c.id for c in chunks])
            raise
        await self.index.retire(source_id, keep_revision=revision)
        return IngestReport(
            IngestStatus.REINDEXED if existing else IngestStatus.CREATED, source_id, revision, len(chunks),
            [c.section_ref for c in chunks],
        )

    async def set_status(self, source_id: uuid.UUID, act: ActMeta, *, actor_user_id: uuid.UUID) -> None:
        """Зміна статусу/строків чинності (напр. акт скасовано) без переіндексації тексту."""
        await self.repo.set_status(source_id, act, actor_user_id)
        await self.index.set_act_status(source_id, act)
