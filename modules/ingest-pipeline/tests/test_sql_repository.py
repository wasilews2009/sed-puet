"""Інтеграція з живим PostgreSQL 16 + pgvector: міграція з ../db-schema, повний пайплайн до БД,
тригери HITL і audit_logs.

    export SED_TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost/sed_ingest_test
    pytest -q tests/test_sql_repository.py
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from sed_db.enums import (
    AccessLevel,
    AiRunStatus,
    AiTaskType,
    AuditEventCategory,
    DocumentStatus,
    HumanReviewStatus,
    OrgUnitKind,
    UserRole,
    VersionSource,
)
from sed_db.models import AiExtractedField, AiRun, AuditLog, Document, DocumentType, DocumentVersion, OrgUnit, User
from sed_db.models import UserRoleAssignment
from sed_ingest.adapters.sql_repository import SqlIngestRepository
from sed_ingest.config import Settings
from sed_ingest.domain.models import (
    AccessDenied,
    ConfigurationError,
    FileKind,
    IncomingFile,
    IngestCommand,
    LlmError,
)
from sed_ingest.domain.requisites import FIELD_KEYS, PII_FIELD_KEYS
from sed_ingest.domain.result import IngestStatus
from sed_ingest.pipeline.extraction import RequisitesExtractor
from sed_ingest.pipeline.orchestrator import IngestPipeline
from sed_ingest.pipeline.pii import RegexPseudonymizer

from .conftest import ORDER_LINES, FakeStorage, ScriptedLlm, make_text_pdf, order_responder

DB_URL = os.environ.get("SED_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="SED_TEST_DATABASE_URL не задано")


@pytest.fixture(scope="module")
def migrated() -> Iterator[str]:
    from alembic import command
    from alembic.config import Config
    from pathlib import Path

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
    """Університет, кафедра, працівник кафедри, чужий підрозділ, довідник видів."""
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s, s.begin():
        tag = uuid.uuid4().hex[:6]
        uni = OrgUnit(kind=OrgUnitKind.UNIVERSITY, code=f"PUET-{tag}", name_uk="ПУЕТ")
        s.add(uni)
        await s.flush()
        dept = OrgUnit(kind=OrgUnitKind.DEPARTMENT, code=f"K-{tag}", name_uk="Кафедра економіки", parent_id=uni.id)
        other = OrgUnit(kind=OrgUnitKind.DEPARTMENT, code=f"O-{tag}", name_uk="Кафедра права", parent_id=uni.id)
        s.add_all([dept, other])
        await s.flush()
        clerk = User(keycloak_sub=uuid.uuid4(), email=f"{tag}@puet.edu.ua", last_name="Діловодченко",
                     first_name="Ганна", primary_org_unit_id=dept.id)
        rector = User(keycloak_sub=uuid.uuid4(), email=f"r{tag}@puet.edu.ua", last_name="Проректоренко",
                      first_name="Андрій", primary_org_unit_id=uni.id)
        s.add_all([clerk, rector])
        await s.flush()
        # Проректор діє на рівні університету (ректор у БД може бути лише один — EXCLUDE).
        s.add(UserRoleAssignment(user_id=rector.id, role=UserRole.VICE_RECTOR, org_unit_id=None,
                                 scope_note="науково-педагогічна робота", valid_from=date(2020, 1, 1)))
        pii = {"type": "object", "properties": {k: {"x-pii": True} for k in PII_FIELD_KEYS}}
        for code, name in (("ORDER_MAIN", "Наказ з основної діяльності"), ("UNSORTED", "Нерозібрані вхідні")):
            if await s.scalar(select(DocumentType.id).where(DocumentType.code == code)) is None:
                s.add(DocumentType(code=code, name_uk=name, requisites_schema=pii,
                                   default_access_level=AccessLevel.INTERNAL))
    return {"dept": dept.id, "other": other.id, "clerk_sub": clerk.keycloak_sub,
            "rector_sub": rector.keycloak_sub, "clerk": clerk.id}


def _pipeline(engine: AsyncEngine, llm: ScriptedLlm, ocr_engine, storage: FakeStorage | None = None):  # type: ignore[no-untyped-def]
    settings = Settings()
    repo = SqlIngestRepository(async_sessionmaker(engine, expire_on_commit=False),
                               settings.document_type_codes, settings.fallback_document_type_code)
    return IngestPipeline(storage or FakeStorage(), ocr_engine,
                          RequisitesExtractor(llm, RegexPseudonymizer(), 24_000), repo, llm.identity), repo


def _cmd(data: bytes, sub: uuid.UUID, unit: uuid.UUID) -> IngestCommand:
    import hashlib
    return IngestCommand(file=IncomingFile(data, "nakaz.pdf", FileKind.PDF, hashlib.sha256(data).hexdigest()),
                         uploader_sub=sub, org_unit_id=unit, direction=None, access_level=None,
                         correspondent_name=None, request_id="it-1")


async def test_full_ingest_persists_document_runs_fields_and_audit(engine, world, ocr_engine) -> None:  # type: ignore[no-untyped-def]
    pdf = make_text_pdf(ORDER_LINES + [f"Примірник {uuid.uuid4().hex[:8]}"])   # унікальний SHA-256
    pipeline, _ = _pipeline(engine, ScriptedLlm(order_responder), ocr_engine)
    res = await pipeline.run(_cmd(pdf, world["clerk_sub"], world["dept"]))
    assert res.status is IngestStatus.PROCESSED and res.document_type_code == "ORDER_MAIN"

    sm = async_sessionmaker(engine)
    async with sm() as s:
        doc = await s.get(Document, res.document_id)
        assert doc is not None and doc.status is DocumentStatus.DRAFT and doc.current_version_id == res.version_id
        assert doc.title == "Про призначення відповідальних за організацію практики"
        assert doc.author_id == world["clerk"] and doc.requisites == {}    # реквізити в картку — лише людиною
        ver = await s.get(DocumentVersion, res.version_id)
        assert ver is not None and ver.source is VersionSource.UPLOAD and "№ 245-од" in (ver.extracted_text or "")
        assert ver.storage_key == res.file.storage_key and ver.content_sha256 == res.file.sha256

        runs = {r.task_type: r for r in (await s.scalars(
            select(AiRun).where(AiRun.document_version_id == res.version_id))).all()}
        ocr_run, ner = runs[AiTaskType.OCR], runs[AiTaskType.NER_EXTRACTION]
        assert ocr_run.status is AiRunStatus.SUCCEEDED and ocr_run.confidence == Decimal("1.0000")
        assert ner.status is AiRunStatus.SUCCEEDED and ner.pii_masked and ner.pii_entities_masked == 3
        assert ner.confidence == res.confidence and ner.human_review_status is HumanReviewStatus.NOT_REQUIRED
        assert ner.prompt_template_id is not None and ner.input_sha256 and ner.output["label"] == "order_main"
        assert "РЕКТОРЕНКО" not in str(ner.output)                     # у журналі запусків — лише мітки

        fields = {f.field_key: f for f in (await s.scalars(
            select(AiExtractedField).where(AiExtractedField.ai_run_id == ner.id))).all()}
        assert set(fields) == set(FIELD_KEYS)
        assert fields["registration_number"].extracted_value == "245-од"
        assert fields["author"].extracted_value["full_name"] == "Іван РЕКТОРЕНКО"   # type: ignore[index]

        audit = (await s.scalars(select(AuditLog).where(
            AuditLog.ai_run_id == ner.id, AuditLog.category == AuditEventCategory.AI_ANALYSIS))).one()
        assert audit.confidence_score == res.confidence and audit.ai_verdict == "order_main"
        assert audit.extracted_metadata["author"]["masked"] is True
        assert audit.extracted_metadata["registration_number"]["value"] == "245-од"
        assert "РЕКТОРЕНКО" not in str(audit.extracted_metadata)
        assert (await s.execute(text("SELECT * FROM sed_audit_verify()"))).all() == []


async def test_low_confidence_lands_in_operator_queue(engine, world, ocr_engine) -> None:  # type: ignore[no-untyped-def]
    def weak(messages):  # type: ignore[no-untyped-def]
        r = order_responder(messages)
        r["registration_number"] = {"value": None, "evidence": None, "confidence": 0.9}
        return r

    pdf = make_text_pdf(ORDER_LINES + [uuid.uuid4().hex])
    pipeline, _ = _pipeline(engine, ScriptedLlm(weak), ocr_engine)
    res = await pipeline.run(_cmd(pdf, world["clerk_sub"], world["dept"]))
    assert res.status is IngestStatus.NEEDS_REVIEW
    async with async_sessionmaker(engine)() as s:
        ner = (await s.scalars(select(AiRun).where(AiRun.id == res.ai_runs.extraction))).one()
        assert ner.human_review_status is HumanReviewStatus.PENDING and ner.requires_human_review
        field = (await s.scalars(select(AiExtractedField).where(
            AiExtractedField.ai_run_id == ner.id, AiExtractedField.field_key == "registration_number"))).one()
        assert field.confidence == 0 and field.human_review_status is HumanReviewStatus.PENDING


async def test_llm_outage_saves_failed_run_in_queue(engine, world, ocr_engine) -> None:  # type: ignore[no-untyped-def]
    pdf = make_text_pdf(ORDER_LINES + [uuid.uuid4().hex])
    pipeline, _ = _pipeline(engine, ScriptedLlm(LlmError("Локальна LLM недоступна")), ocr_engine)
    res = await pipeline.run(_cmd(pdf, world["clerk_sub"], world["dept"]))
    assert res.status is IngestStatus.MANUAL_ENTRY_REQUIRED and res.document_type_code == "UNSORTED"
    async with async_sessionmaker(engine)() as s:
        ner = await s.get(AiRun, res.ai_runs.extraction)
        assert ner is not None and ner.status is AiRunStatus.FAILED
        assert ner.error_message == "Локальна LLM недоступна" and ner.human_review_status is HumanReviewStatus.PENDING


async def test_access_rules(engine, world) -> None:  # type: ignore[no-untyped-def]
    _, repo = _pipeline(engine, ScriptedLlm(order_responder), None)
    with pytest.raises(AccessDenied, match="Немає права"):
        await repo.resolve_uploader(world["clerk_sub"], world["other"])
    with pytest.raises(AccessDenied, match="не знайдено"):
        await repo.resolve_uploader(uuid.uuid4(), world["dept"])
    assert (await repo.resolve_uploader(world["rector_sub"], world["other"])).org_unit_id == world["other"]


async def test_db_error_rolls_back_and_cleans_storage(engine, world, ocr_engine) -> None:  # type: ignore[no-untyped-def]
    storage = FakeStorage()
    pipeline, repo = _pipeline(engine, ScriptedLlm(order_responder), ocr_engine, storage)
    repo.fallback_type_code = "NO_SUCH_TYPE"
    repo.type_codes = {}
    pdf = make_text_pdf(ORDER_LINES + [uuid.uuid4().hex])
    with pytest.raises(ConfigurationError):
        await pipeline.run(_cmd(pdf, world["clerk_sub"], world["dept"]))
    assert storage.objects == {} and len(storage.deleted) == 1
    async with async_sessionmaker(engine)() as s:
        import hashlib
        n = await s.scalar(select(func.count()).select_from(DocumentVersion).where(
            DocumentVersion.content_sha256 == hashlib.sha256(pdf).hexdigest()))
        assert n == 0


async def test_duplicate_detected_on_second_upload(engine, world, ocr_engine) -> None:  # type: ignore[no-untyped-def]
    pdf = make_text_pdf(ORDER_LINES + [uuid.uuid4().hex])
    pipeline, _ = _pipeline(engine, ScriptedLlm(order_responder), ocr_engine)
    first = await pipeline.run(_cmd(pdf, world["clerk_sub"], world["dept"]))
    second = await pipeline.run(_cmd(pdf, world["clerk_sub"], world["dept"]))
    assert first.duplicate_of is None and second.duplicate_of == first.document_id
