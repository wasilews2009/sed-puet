"""Інтеграція з живим PostgreSQL 16 + pgvector: міграції з ../db-schema, корпус у regulatory_*,
перевірка версії документа → ai_runs + compliance_* + audit_logs (тригери HITL і журналу).

    export SED_TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost/sed_rag_test
    pytest -q tests/test_sql.py
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from sed_db.enums import (
    AccessLevel,
    AiRunStatus,
    AiTaskType,
    AuditEventCategory,
    DocumentDirection,
    DocumentStatus,
    HumanReviewStatus,
    OrgUnitKind,
    VersionSource,
)
from sed_db.models import (
    AiRun,
    AuditLog,
    ComplianceCheck,
    ComplianceCitation,
    ComplianceFinding,
    Document,
    DocumentType,
    DocumentVersion,
    OrgUnit,
    PromptTemplate,
    RegulatoryChunk,
    RegulatorySource,
    User,
)
from sed_db.seeds import SYSTEM_USER_ID
from sed_ingest.pipeline.pii import RegexPseudonymizer
from sed_rag.adapters.sql_compliance import SqlComplianceRepository
from sed_rag.adapters.sql_corpus import SqlCorpusRepository
from sed_rag.corpus.sparse import Bm25Encoder
from sed_rag.domain.models import AccessDenied, NotFound
from sed_rag.ingestion.service import CorpusIngestor
from sed_rag.retrieval.hybrid import HybridRetriever
from sed_rag.service import ComplianceService

from .conftest import AS_OF, CORPUS, FIXTURES, ORDER_TEXT, STATUTE, RuleLlm, make_auditor, settings

DB_URL = os.environ.get("SED_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="SED_TEST_DATABASE_URL не задано")


@pytest.fixture(scope="module")
def migrated() -> Iterator[str]:
    from alembic import command
    from alembic.config import Config

    url = make_url(DB_URL)  # type: ignore[arg-type]
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'))
        c.execute(text(f'CREATE DATABASE "{url.database}"'))
    admin.dispose()
    schema_dir = Path(__file__).resolve().parents[2] / "db-schema"
    os.environ["SED_DATABASE_URL"] = url.render_as_string(hide_password=False)
    cfg = Config(str(schema_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(schema_dir / "migrations"))
    command.upgrade(cfg, "head")
    yield url.render_as_string(hide_password=False)


@pytest.fixture()
async def engine(migrated: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated)
    yield eng
    await eng.dispose()


@pytest.fixture()
async def world(engine: AsyncEngine) -> dict[str, uuid.UUID]:
    """Підрозділ, автор проєкту, сторонній користувач, проєкт наказу (версія 1 з текстом)."""
    sm = async_sessionmaker(engine, expire_on_commit=False)
    tag = uuid.uuid4().hex[:6]
    async with sm() as s, s.begin():
        uni = OrgUnit(kind=OrgUnitKind.UNIVERSITY, code=f"PUET-{tag}", name_uk="ПУЕТ")
        s.add(uni)
        await s.flush()
        dept = OrgUnit(kind=OrgUnitKind.DEPARTMENT, code=f"K-{tag}", name_uk="Кафедра економіки", parent_id=uni.id)
        other = OrgUnit(kind=OrgUnitKind.DEPARTMENT, code=f"O-{tag}", name_uk="Кафедра права", parent_id=uni.id)
        s.add_all([dept, other])
        await s.flush()
        author = User(keycloak_sub=uuid.uuid4(), email=f"a{tag}@puet.edu.ua", last_name="Автор", first_name="Ганна",
                      primary_org_unit_id=dept.id)
        stranger = User(keycloak_sub=uuid.uuid4(), email=f"s{tag}@puet.edu.ua", last_name="Сторонній",
                        first_name="Петро", primary_org_unit_id=other.id)
        s.add_all([author, stranger])
        await s.flush()
        await s.execute(text("SELECT set_config('sed.actor_user_id', :u, true), set_config('sed.actor_type', 'user', true)"),
                        {"u": str(author.id)})
        dtype = await s.scalar(select(DocumentType).where(DocumentType.code == "ORDER_MAIN"))
        assert dtype is not None
        doc = Document(document_type_id=dtype.id, direction=DocumentDirection.INTERNAL, status=DocumentStatus.DRAFT,
                       access_level=AccessLevel.INTERNAL, title="Про призначення завідувача кафедри економіки",
                       author_id=author.id, org_unit_id=dept.id)
        s.add(doc)
        await s.flush()
        ver = DocumentVersion(document_id=doc.id, version_number=1, source=VersionSource.UPLOAD,
                              storage_bucket="sed-documents", storage_key=f"documents/{doc.id}/v1.pdf",
                              file_name="nakaz.pdf", mime_type="application/pdf", size_bytes=1000,
                              content_sha256=hashlib.sha256(ORDER_TEXT.encode()).hexdigest(),
                              extracted_text=ORDER_TEXT, created_by_id=author.id)
        s.add(ver)
        await s.flush()
        doc.current_version_id = ver.id
    return {"author_sub": author.keycloak_sub, "stranger_sub": stranger.keycloak_sub, "version": ver.id,
            "document": doc.id}


@pytest.fixture()
async def sql_corpus(engine: AsyncEngine, index, embedder) -> CorpusIngestor:  # type: ignore[no-untyped-def]
    ing = CorpusIngestor(repository=SqlCorpusRepository(async_sessionmaker(engine, expire_on_commit=False)),
                         index=index, embedder=embedder, bm25=Bm25Encoder())
    for act, file in CORPUS:
        await ing.ingest(act, (FIXTURES / file).read_text(encoding="utf-8"), actor_user_id=SYSTEM_USER_ID, force=True)
    return ing


def _service(engine: AsyncEngine, index, embedder, llm: RuleLlm) -> ComplianceService:  # type: ignore[no-untyped-def]
    s = settings()
    repo = SqlComplianceRepository(async_sessionmaker(engine, expire_on_commit=False), RegexPseudonymizer())
    return ComplianceService(make_auditor(index, embedder, llm, s),
                             HybridRetriever(index, embedder, Bm25Encoder(), s.retrieval), repo)


async def test_corpus_rows_match_qdrant_points(engine, sql_corpus, index) -> None:  # type: ignore[no-untyped-def]
    sm = async_sessionmaker(engine)
    async with sm() as s:
        n_current = await s.scalar(select(func.count()).select_from(RegulatoryChunk).where(RegulatoryChunk.is_current))
        src = await s.scalar(select(RegulatorySource).where(RegulatorySource.title == STATUTE.title))
        no_vec = await s.scalar(select(func.count()).select_from(RegulatoryChunk).where(RegulatoryChunk.embedding.is_not(None)))
    assert src is not None and src.current_index_revision >= 1 and len(src.content_sha256 or "") == 64
    assert no_vec == 0                                      # dense-вектори живуть у Qdrant
    cat_ids = {c.first_chunk_id for c in await index.catalog()}
    async with sm() as s:
        db_ids = set((await s.scalars(select(RegulatoryChunk.id).where(RegulatoryChunk.id.in_(cat_ids)))).all())
    assert db_ids == cat_ids and n_current >= len(cat_ids)


async def test_check_version_writes_run_findings_citations_and_audit(engine, world, sql_corpus, index, embedder) -> None:  # type: ignore[no-untyped-def]
    llm = RuleLlm()
    rep = await _service(engine, index, embedder, llm).check_version(world["author_sub"], world["version"],
                                                                      request_id="req-42")
    assert rep.ai_run_id and rep.check_id and rep.regulations_as_of == date.today()

    sm = async_sessionmaker(engine)
    async with sm() as s:
        run = await s.get(AiRun, rep.ai_run_id)
        assert run is not None
        assert run.task_type is AiTaskType.COMPLIANCE_CHECK and run.status is AiRunStatus.SUCCEEDED
        assert run.pii_masked and run.pii_entities_masked and run.pii_entities_masked >= 3
        assert run.human_review_status is HumanReviewStatus.PENDING          # < 0.85 → черга оператора
        assert run.requires_human_review and run.document_id == world["document"]
        assert "Петренко" not in json.dumps(run.output, ensure_ascii=False)
        prompt = await s.get(PromptTemplate, run.prompt_template_id)
        assert prompt is not None and prompt.code == "compliance.assess_provision"
        assert set(run.parameters["prompts"]) == {"compliance.assess_provision", "compliance.verify_finding",
                                                  "compliance.rewrite_query"}

        check = await s.scalar(select(ComplianceCheck).where(ComplianceCheck.ai_run_id == run.id))
        assert check is not None and check.verdict == rep.verdict and check.retrieval_config["fusion"] == "rrf"
        findings = (await s.scalars(select(ComplianceFinding).where(ComplianceFinding.check_id == check.id)
                                    .order_by(ComplianceFinding.ordinal))).all()
        assert len(findings) == len(rep.findings) >= 3
        f0 = findings[0]
        assert f0.severity == rep.findings[0].severity and f0.requires_human_review == rep.findings[0].requires_human_review
        spans = [(f.char_start, f.char_end) for f in findings if f.char_start is not None]
        assert any(ORDER_TEXT[a:b] == "строком на 7 років без проведення конкурсу" for a, b in spans)

        cits = (await s.execute(
            select(ComplianceCitation, RegulatoryChunk.section_ref, RegulatoryChunk.content)
            .join(RegulatoryChunk, RegulatoryChunk.id == ComplianceCitation.chunk_id)
            .where(ComplianceCitation.finding_id.in_([f.id for f in findings])))).all()
        assert len(cits) == len(findings)
        for c, _ref, content in cits:
            assert c.quote_verified and c.quoted_text in content           # цитата буквально є у фрагменті БД
        assert {r for _, r, _ in cits} >= {"п. 7.2.1", "п. 12.1"}

        audit = await s.scalar(select(AuditLog).where(AuditLog.ai_run_id == run.id))
        assert audit is not None and audit.category is AuditEventCategory.AI_ANALYSIS
        assert audit.ai_verdict == rep.verdict.value and audit.requires_human_review
        sections = {c["section"] for v in audit.violations for c in v["citations"]}
        assert {"п. 7.2.1", "п. 12.1"} <= sections
        assert audit.request_id == "req-42"
        assert "Петренко" not in json.dumps(audit.violations, ensure_ascii=False)
        assert await s.scalar(text("SELECT count(*) FROM sed_audit_verify()")) == 0


async def test_history_survives_reindex(engine, world, sql_corpus, index, embedder) -> None:  # type: ignore[no-untyped-def]
    rep = await _service(engine, index, embedder, RuleLlm()).check_version(world["author_sub"], world["version"])
    old_chunk = next(f.citations[0].chunk_id for f in rep.findings if f.citations[0].section_ref == "п. 7.2.1")
    changed = (FIXTURES / "statut_SAMPLE.txt").read_text().replace("строком на п'ять років", "строком на шість років")
    await sql_corpus.ingest(STATUTE, changed, actor_user_id=SYSTEM_USER_ID)
    async with async_sessionmaker(engine)() as s:
        old = await s.get(RegulatoryChunk, old_chunk)
        assert old is not None and not old.is_current                         # фрагмент не видалено
        n = await s.scalar(select(func.count()).select_from(ComplianceCitation)
                           .where(ComplianceCitation.chunk_id == old_chunk))
    assert n >= 1


async def test_access_control(engine, world, sql_corpus, index, embedder) -> None:  # type: ignore[no-untyped-def]
    svc = _service(engine, index, embedder, RuleLlm())
    with pytest.raises(AccessDenied):
        await svc.check_version(world["stranger_sub"], world["version"])
    with pytest.raises(AccessDenied):
        await svc.check_version(uuid.uuid4(), world["version"])
    with pytest.raises(NotFound):
        await svc.check_version(world["author_sub"], uuid.uuid4())


async def test_ai_actor_cannot_resolve_findings(engine, world, sql_corpus, index, embedder) -> None:  # type: ignore[no-untyped-def]
    """Guard-тригер БД: роль ШІ не може закрити власне зауваження (HITL на рівні БД)."""
    rep = await _service(engine, index, embedder, RuleLlm()).check_version(world["author_sub"], world["version"])
    from sqlalchemy.exc import DBAPIError

    sm = async_sessionmaker(engine)
    with pytest.raises(DBAPIError, match="cannot resolve compliance findings"):
        async with sm() as s, s.begin():
            await s.execute(text("SELECT set_config('sed.actor_type', 'ai_service', true)"))
            await s.execute(text("UPDATE compliance_findings SET status = 'dismissed', resolved_by_id = :u, "
                                 "resolved_at = now(), resolution_comment = 'x' WHERE check_id = :c"),
                            {"u": str(SYSTEM_USER_ID), "c": str(rep.check_id)})
