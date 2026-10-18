"""Реалізації портів у пам'яті: для тестів і локальної роботи з корпусом без PostgreSQL
(`cli … --no-db`; цитати тоді не можна зберегти в `compliance_citations`)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace

from ..domain.models import ActMeta, ChunkRecord, NotFound, SourceRecord


class MemoryCorpusRepository:
    def __init__(self) -> None:
        self.sources: dict[uuid.UUID, SourceRecord] = {}
        self.chunks: dict[uuid.UUID, ChunkRecord] = {}
        self.current: set[uuid.UUID] = set()

    async def find_source(self, act: ActMeta) -> SourceRecord | None:
        for s in self.sources.values():
            m = s.meta
            if (m.level, m.issuer, m.title, m.doc_number) == (act.level, act.issuer, act.title, act.doc_number):
                return s
        return None

    @asynccontextmanager
    async def save_revision(self, source: SourceRecord, chunks: Sequence[ChunkRecord],
                            actor_user_id: uuid.UUID) -> AsyncIterator[None]:
        yield
        self.sources[source.id] = source
        self.current -= {c.id for c in self.chunks.values() if c.source_id == source.id}
        for c in chunks:
            self.chunks[c.id] = c
            self.current.add(c.id)

    async def set_status(self, source_id: uuid.UUID, act: ActMeta, actor_user_id: uuid.UUID) -> None:
        if source_id not in self.sources:
            raise NotFound("Нормативний акт не знайдено")
        self.sources[source_id] = replace(self.sources[source_id], meta=act)
