"""Порти (інтерфейси) інфраструктури. Граф перевірки і індексатор залежать лише від них:
Qdrant, сервер ембедингів, reranker, LLM і PostgreSQL підміняються в тестах і замінюються
(напр. Qdrant → pgvector) без зміни домену.

`StructuredLlm` і `PiiMasker` — ті самі порти, що в `sed_ingest`: обидва модулі
ходять в одну локальну LLM і однаково псевдонімізують ПД.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol

from sed_ingest.ports import PiiMasker, StructuredLlm

from .corpus.sparse import SparseVector
from .domain.models import ActMeta, ChunkRecord, SourceRecord
from .domain.report import ComplianceReport

__all__ = [
    "CheckRecord", "ComplianceRepository", "CorpusRepository", "Embedder", "IndexedSource", "PiiMasker",
    "Reranker", "SavedCheck", "ScoredPoint", "StructuredLlm", "VectorIndex", "VersionForCheck",
]


class Embedder(Protocol):
    model_name: str
    model_version: str
    dim: int

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


class Reranker(Protocol):
    model_name: str

    async def rerank(self, query: str, texts: Sequence[str]) -> list[float]:
        """Оцінки релевантності 0…1 у порядку `texts`."""
        ...


@dataclass(frozen=True, slots=True)
class ScoredPoint:
    chunk_id: uuid.UUID
    score: float
    payload: dict  # type: ignore[type-arg]


@dataclass(frozen=True, slots=True)
class IndexedSource:
    """Акт у каталозі індексу (для перевірки посилань проєкту на інші акти)."""

    source_id: uuid.UUID
    meta: ActMeta
    first_chunk_id: uuid.UUID
    first_chunk_ref: str
    first_chunk_text: str


class VectorIndex(Protocol):
    async def ensure_collection(self, dim: int) -> None: ...

    async def upsert(self, act: ActMeta, source_id: uuid.UUID, chunks: Sequence[ChunkRecord],
                     dense: Sequence[Sequence[float]], sparse: Sequence[SparseVector]) -> None: ...

    async def retire(self, source_id: uuid.UUID, keep_revision: int) -> None:
        """Позначити фрагменти старих ревізій `is_current = false` (не видаляючи: на них є цитати)."""
        ...

    async def delete_points(self, ids: Sequence[uuid.UUID]) -> None: ...

    async def set_act_status(self, source_id: uuid.UUID, act: ActMeta) -> None: ...

    async def search_dense(self, vector: Sequence[float], as_of: date, limit: int) -> list[ScoredPoint]: ...

    async def search_sparse(self, vector: SparseVector, as_of: date, limit: int) -> list[ScoredPoint]: ...

    async def catalog(self) -> list[IndexedSource]:
        """Усі акти поточних ревізій, зокрема нечинні (щоб упіймати посилання на скасований акт)."""
        ...

    async def count(self) -> int: ...


class CorpusRepository(Protocol):
    """Нормативний корпус у PostgreSQL (`regulatory_sources`, `regulatory_chunks`) — джерело
    істини для цитат: `compliance_citations.chunk_id` посилається саме сюди."""

    async def find_source(self, act: ActMeta) -> SourceRecord | None: ...

    def save_revision(self, source: SourceRecord, chunks: Sequence[ChunkRecord],
                      actor_user_id: uuid.UUID) -> AbstractAsyncContextManager[None]:
        """Транзакція нової ревізії: вихід з контексту без помилки — COMMIT, інакше ROLLBACK.
        Індекс оновлюється всередині контексту, тож збій Qdrant відкочує й БД."""
        ...

    async def set_status(self, source_id: uuid.UUID, act: ActMeta, actor_user_id: uuid.UUID) -> None: ...


@dataclass(frozen=True, slots=True)
class VersionForCheck:
    user_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    text: str
    title: str | None
    document_type_code: str
    document_date: date | None


@dataclass(frozen=True, slots=True)
class CheckRecord:
    """Усе, що потрібно зберегти для перевірки версії документа (одна транзакція)."""

    version: VersionForCheck
    report: ComplianceReport
    llm_name: str
    llm_version: str
    llm_provider: str
    prompts: Sequence[tuple[str, int, str]]     # (code, version, body); перший — основний промпт
    input_sha256: str
    pii_entities_masked: int
    raw_output: dict[str, Any]                  # відповіді моделі з мітками ПД (без сирих ПД)
    tokens_in: int | None
    tokens_out: int | None
    latency_ms: int
    started_at: datetime
    finished_at: datetime
    request_id: str | None


@dataclass(frozen=True, slots=True)
class SavedCheck:
    ai_run_id: uuid.UUID
    check_id: uuid.UUID


class ComplianceRepository(Protocol):
    async def load_version(self, keycloak_sub: uuid.UUID, version_id: uuid.UUID) -> VersionForCheck:
        """Версія документа з текстом; кидає `AccessDenied` / `NotFound`."""
        ...

    async def save_check(self, record: CheckRecord) -> SavedCheck: ...
