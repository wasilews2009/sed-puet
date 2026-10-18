"""Нормативний корпус у PostgreSQL (`regulatory_sources`, `regulatory_chunks`) через моделі `sed_db`.

БД — джерело істини для цитат (`compliance_citations.chunk_id` → `regulatory_chunks.id`), Qdrant —
пошуковий індекс тих самих фрагментів (point id = chunk id). Стовпець `embedding` (pgvector)
лишається NULL: dense-вектори живуть у Qdrant.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sed_db.models import RegulatoryChunk, RegulatorySource, User

from ..domain.models import AccessDenied, ActMeta, ChunkRecord, NotFound, SourceRecord


async def set_actor(s: AsyncSession, user_id: uuid.UUID, actor_type: str, request_id: str | None = None) -> None:
    await s.execute(
        text("SELECT set_config('sed.actor_user_id', :u, true), set_config('sed.actor_type', :t, true),"
             "       set_config('sed.request_id', :r, true)"),
        {"u": str(user_id), "t": actor_type, "r": request_id or ""},
    )


def _meta_from_row(row: RegulatorySource, like: ActMeta) -> ActMeta:
    # short_title / is_sample у таблиці немає (див. README, «Що потрібно від схеми БД») — беремо з запиту.
    return ActMeta(
        title=row.title, level=row.level, issuer=row.issuer, status=row.status, doc_number=row.doc_number,
        adopted_on=row.adopted_on, effective_from=row.effective_from, effective_to=row.effective_to,
        short_title=like.short_title, source_url=row.source_url, is_sample=like.is_sample,
    )


class SqlCorpusRepository:
    """Реалізація порту `CorpusRepository`."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self.sessionmaker = sessionmaker

    async def find_source(self, act: ActMeta) -> SourceRecord | None:
        async with self.sessionmaker() as s:
            row = await s.scalar(
                select(RegulatorySource).where(
                    RegulatorySource.level == act.level, RegulatorySource.issuer == act.issuer,
                    RegulatorySource.title == act.title,
                    RegulatorySource.doc_number.is_not_distinct_from(act.doc_number),
                ).order_by(RegulatorySource.created_at.desc()).limit(1)
            )
            if row is None:
                return None
            return SourceRecord(row.id, _meta_from_row(row, act), row.current_index_revision, row.content_sha256 or "")

    @asynccontextmanager
    async def save_revision(self, source: SourceRecord, chunks: Sequence[ChunkRecord],
                            actor_user_id: uuid.UUID) -> AsyncIterator[None]:
        act = source.meta
        async with self.sessionmaker() as s, s.begin():
            await set_actor(s, actor_user_id, "system")
            row = await s.get(RegulatorySource, source.id, with_for_update=True)
            if row is None:
                row = RegulatorySource(id=source.id)
                s.add(row)
            row.level, row.issuer, row.title, row.doc_number = act.level, act.issuer, act.title, act.doc_number
            row.adopted_on, row.effective_from, row.effective_to = act.adopted_on, act.effective_from, act.effective_to
            row.status, row.source_url = act.status, act.source_url
            row.content_sha256, row.current_index_revision = source.content_sha256, source.index_revision
            await s.flush()
            await s.execute(update(RegulatoryChunk)
                            .where(RegulatoryChunk.source_id == source.id, RegulatoryChunk.is_current.is_(True))
                            .values(is_current=False))
            s.add_all([
                RegulatoryChunk(
                    id=c.id, source_id=source.id, index_revision=c.index_revision, chunk_no=c.chunk_no,
                    section_ref=c.section_ref[:256], content=c.content, token_count=c.token_count, is_current=True,
                )
                for c in chunks
            ])
            await s.flush()
            yield                       # тут оновлюється Qdrant; виняток → ROLLBACK

    async def set_status(self, source_id: uuid.UUID, act: ActMeta, actor_user_id: uuid.UUID) -> None:
        async with self.sessionmaker() as s, s.begin():
            await set_actor(s, actor_user_id, "system")
            row = await s.get(RegulatorySource, source_id, with_for_update=True)
            if row is None:
                raise NotFound("Нормативний акт не знайдено")
            row.status, row.effective_from, row.effective_to = act.status, act.effective_from, act.effective_to

    async def resolve_user(self, keycloak_sub: uuid.UUID) -> uuid.UUID:
        async with self.sessionmaker() as s:
            row = (await s.execute(select(User.id, User.is_active).where(User.keycloak_sub == keycloak_sub))).one_or_none()
        if row is None or not row.is_active:
            raise AccessDenied("Обліковий запис не знайдено в СЕД або його деактивовано")
        return row.id
