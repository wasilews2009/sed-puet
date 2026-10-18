"""Seed-міграція 0002: довідник видів, позначки ПД, промпт конвеєра завантаження."""

from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from sed_db.enums import AccessLevel, AiRunStatus, AiTaskType, ModelProvider, VersionSource
from sed_db.models import AiExtractedField, AiModel, AiRun, AuditLog, Document, DocumentType, DocumentVersion, PromptTemplate, User
from sed_db.seeds import (
    DOCUMENT_TYPES,
    PII_FIELD_KEYS,
    PROMPTS,
    SYSTEM_USER_ID,
    UNSORTED_TYPE_CODE,
    load_prompt,
)

INGEST_DIR = Path(__file__).resolve().parents[2] / "ingest-pipeline"


def test_document_types_seeded_with_pii_marks(session: Session) -> None:
    types = {t.code: t for t in session.scalars(select(DocumentType))}
    assert {c for c, *_ in DOCUMENT_TYPES} <= types.keys()
    assert types[UNSORTED_TYPE_CODE].is_active
    assert types[UNSORTED_TYPE_CODE].default_access_level is AccessLevel.RESTRICTED
    for code, *_ in DOCUMENT_TYPES:
        props = types[code].requisites_schema["properties"]
        assert {k for k, v in props.items() if v.get("x-pii")} == PII_FIELD_KEYS, code


def test_prompt_and_system_user_seeded(session: Session) -> None:
    system = session.get(User, SYSTEM_USER_ID)
    assert system is not None and not system.is_active
    for code, version, _task, file_name, sha in PROMPTS:
        row = session.scalars(select(PromptTemplate).where(
            PromptTemplate.code == code, PromptTemplate.version == version)).one()
        assert row.body_sha256 == sha == hashlib.sha256(row.body.encode()).hexdigest()
        assert row.body == load_prompt(file_name, sha) and row.created_by_id == SYSTEM_USER_ID


def test_seed_matches_ingest_pipeline_constants() -> None:
    """Якщо поруч є модуль інжесту — його коди, ключі ПД і промпт мають збігатися з seed."""
    if not INGEST_DIR.is_dir():
        pytest.skip("ingest-pipeline поруч немає")
    sys.path.insert(0, str(INGEST_DIR))
    try:
        from sed_ingest.config import Settings  # type: ignore[import-not-found]
        from sed_ingest.domain.requisites import PII_FIELD_KEYS as INGEST_PII  # type: ignore[import-not-found]
        from sed_ingest.pipeline.prompts import PROMPT_CODE, PROMPT_VERSION, SYSTEM_PROMPT  # type: ignore[import-not-found]
    except ImportError as exc:
        pytest.skip(f"залежності ingest-pipeline не встановлено: {exc}")
    finally:
        sys.path.remove(str(INGEST_DIR))
    settings = Settings()
    assert set(settings.document_type_codes.values()) | {settings.fallback_document_type_code} <= {
        c for c, *_ in DOCUMENT_TYPES}
    assert INGEST_PII == PII_FIELD_KEYS
    seeded = {(c, v): sha for c, v, _t, _f, sha in PROMPTS}
    assert seeded[(PROMPT_CODE, PROMPT_VERSION)] == hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()


def test_author_masked_in_audit_logs_with_seeded_schema(session: Session, world: dict) -> None:
    w = world
    doc = Document(document_type_id=w["order_type"].id, direction=w["direction"],
                   access_level=AccessLevel.INTERNAL, title="Наказ", author_id=w["author"].id,
                   org_unit_id=w["dept"].id)
    session.add(doc)
    session.flush()
    ver = DocumentVersion(document_id=doc.id, version_number=1, source=VersionSource.UPLOAD,
                          storage_bucket="b", storage_key=f"{doc.id}.pdf", file_name="n.pdf",
                          mime_type="application/pdf", size_bytes=1, content_sha256="a" * 64,
                          created_by_id=w["author"].id)
    model = AiModel(name="seed-test-ner", version="1", provider=ModelProvider.VLLM,
                    task_type=AiTaskType.NER_EXTRACTION)
    session.add_all([ver, model])
    session.flush()
    run = AiRun(task_type=AiTaskType.NER_EXTRACTION, status=AiRunStatus.SUCCEEDED, document_id=doc.id,
                document_version_id=ver.id, model_id=model.id, pii_masked=True,
                confidence=Decimal("0.95"), finished_at=datetime.now(timezone.utc))
    session.add(run)
    session.flush()
    session.add_all([
        AiExtractedField(ai_run_id=run.id, document_version_id=ver.id, field_key=k, extracted_value=v,
                         confidence=Decimal("0.95"))
        for k, v in (("author", {"full_name": "Олена Петренко", "position": "доцент"}),
                     ("interested_parties", [{"full_name": "Андрій Коваль", "role": "executor"}]),
                     ("registration_number", "245-од"))
    ])
    session.flush()
    session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    meta = session.scalars(select(AuditLog.extracted_metadata).where(AuditLog.ai_run_id == run.id)).one()
    assert meta["author"]["masked"] and meta["interested_parties"]["masked"]
    assert meta["registration_number"]["value"] == "245-од"
    assert "Петренко" not in str(meta) and "Коваль" not in str(meta)
