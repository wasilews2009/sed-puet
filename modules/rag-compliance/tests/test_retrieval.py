"""Індексація в Qdrant (in-memory, справжній клієнт) і гібридний пошук: ревізії, чинність, компенсація."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import date

import pytest

from sed_db.enums import RegulatoryStatus
from sed_rag.adapters.memory import MemoryCorpusRepository
from sed_rag.adapters.qdrant_index import QdrantVectorIndex
from sed_rag.corpus.sparse import Bm25Encoder
from sed_rag.domain.models import ConfigurationError, Coverage, InvalidSource
from sed_rag.ingestion.service import CorpusIngestor, IngestStatus
from sed_rag.retrieval.hybrid import HybridRetriever

from .conftest import AS_OF, FIXTURES, FUTURE, STATUTE, STYPENDII, SYSTEM_USER, HashEmbedder, settings


def _retriever(index: QdrantVectorIndex, embedder: HashEmbedder, **kw) -> HybridRetriever:  # type: ignore[no-untyped-def]
    return HybridRetriever(index, embedder, Bm25Encoder(), settings(**kw).retrieval)


async def test_hybrid_search_finds_exact_clause(corpus, index, embedder) -> None:  # type: ignore[no-untyped-def]
    r = _retriever(index, embedder)
    res = await r.retrieve("завідувач кафедри обирається за конкурсом строк п'ять років", AS_OF)
    assert res.coverage is Coverage.STRONG
    top = res.clauses[0]
    assert top.citation_label in {"Статут ПУЕТ, п. 7.2.1", "ЗУ «Про вищу освіту», ст. 35, ч. 2"}
    assert top.dense_rank is not None and top.sparse_rank is not None and top.fused_score > 0.9
    labels = [c.citation_label for c in res.clauses]
    assert "Статут ПУЕТ, п. 7.2.1" in labels[:3] and "ЗУ «Про вищу освіту», ст. 35, ч. 2" in labels[:3]


async def test_search_excludes_acts_not_in_force(corpus, ingestor, index, embedder) -> None:  # type: ignore[no-untyped-def]
    await ingestor.ingest(FUTURE, "1. ЗАГАЛЬНІ ПОЛОЖЕННЯ\n1.1. Дуальна форма здобуття освіти стипендіальна комісія.",
                          actor_user_id=SYSTEM_USER)
    r = _retriever(index, embedder)
    q = "стипендіальна комісія рейтинг здобувачів"
    now = [c.act.label for c in (await r.retrieve(q, AS_OF)).clauses]
    assert STYPENDII.label not in now and FUTURE.label not in now          # скасований і ще не чинний
    then = [c.act.label for c in (await r.retrieve(q, date(2023, 1, 10))).clauses]
    assert STYPENDII.label in then                                          # «заднім числом» — чинний
    later = [c.act.label for c in (await r.retrieve(q, date(2027, 2, 1))).clauses]
    assert FUTURE.label in later


async def test_diversity_limit_per_source(corpus, index, embedder) -> None:  # type: ignore[no-untyped-def]
    r = _retriever(index, embedder, max_per_source=2, final_k=6)
    res = await r.retrieve("Університет Статут ректор кафедра вчена рада", AS_OF)
    counts: dict[uuid.UUID, int] = {}
    for c in res.clauses:
        counts[c.source_id] = counts.get(c.source_id, 0) + 1
    assert max(counts.values()) <= 2


async def test_reindex_creates_revision_and_keeps_history(ingestor, index, embedder, corpus_repo) -> None:  # type: ignore[no-untyped-def]
    text = (FIXTURES / "statut_SAMPLE.txt").read_text(encoding="utf-8")
    first = await ingestor.ingest(STATUTE, text, actor_user_id=SYSTEM_USER)
    again = await ingestor.ingest(STATUTE, text, actor_user_id=SYSTEM_USER)
    assert first.status is IngestStatus.CREATED and again.status is IngestStatus.UNCHANGED
    n_points = await index.count()

    changed = text.replace("строком на п'ять років", "строком на шість років")
    second = await ingestor.ingest(STATUTE, changed, actor_user_id=SYSTEM_USER)
    assert second.status is IngestStatus.REINDEXED and second.index_revision == 2 and second.source_id == first.source_id
    assert await index.count() == 2 * n_points                       # стара ревізія лишилась (для історичних цитат)
    r = _retriever(index, embedder)
    hits = (await r.retrieve("завідувач кафедри строком років", AS_OF)).clauses
    contents = " ".join(c.content for c in hits if c.act.label == STATUTE.label)
    assert "шість" in contents and "п'ять" not in contents          # шукаються лише актуальні фрагменти
    assert len(corpus_repo.current) == second.chunks


async def test_qdrant_failure_rolls_back_and_db_failure_compensates(index, embedder) -> None:  # type: ignore[no-untyped-def]
    class FailingRepo(MemoryCorpusRepository):
        def __init__(self) -> None:
            super().__init__()
            self.fail = False

        def save_revision(self, source, chunks, actor_user_id):  # type: ignore[no-untyped-def]
            outer = super().save_revision(source, chunks, actor_user_id)
            repo = self

            class Ctx:
                async def __aenter__(self):  # type: ignore[no-untyped-def]
                    return await outer.__aenter__()

                async def __aexit__(self, *exc):  # type: ignore[no-untyped-def]
                    if repo.fail and exc[0] is None:
                        raise RuntimeError("COMMIT failed")
                    return await outer.__aexit__(*exc)
            return Ctx()

    repo = FailingRepo()
    ing = CorpusIngestor(repository=repo, index=index, embedder=embedder, bm25=Bm25Encoder())
    repo.fail = True
    with pytest.raises(RuntimeError):
        await ing.ingest(STATUTE, (FIXTURES / "statut_SAMPLE.txt").read_text(), actor_user_id=SYSTEM_USER)
    assert await index.count() == 0 and not repo.sources          # точки видалено, БД не змінилась


async def test_status_change_updates_index(corpus, ingestor, index, embedder) -> None:  # type: ignore[no-untyped-def]
    repealed = replace(STATUTE, status=RegulatoryStatus.REPEALED, effective_to=date(2026, 1, 1))
    await ingestor.set_status(corpus[STATUTE.label], repealed, actor_user_id=SYSTEM_USER)
    r = _retriever(index, embedder)
    labels = {c.act.label for c in (await r.retrieve("завідувач кафедри конкурс", AS_OF)).clauses}
    assert STATUTE.label not in labels
    cat = {s.meta.label: s.meta.status for s in await index.catalog()}
    assert cat[STATUTE.label] is RegulatoryStatus.REPEALED


async def test_empty_text_and_dimension_mismatch(ingestor, index) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(InvalidSource):
        await ingestor.ingest(STATUTE, "   \n  ", actor_user_id=SYSTEM_USER)
    await index.ensure_collection(1024)
    with pytest.raises(ConfigurationError):
        await index.ensure_collection(768)


async def test_catalog_lists_current_acts(corpus, index) -> None:  # type: ignore[no-untyped-def]
    cat = await index.catalog()
    assert {s.meta.label for s in cat} == set(corpus)
    assert all(s.first_chunk_text for s in cat)
