"""ШІ-контур: реєстр моделей, журнал запусків, NER-реквізити, нормативний корпус RAG
та логи комплаєнс-перевірок із цитатами-першоджерелами.

Принцип Human-in-the-loop закладено на рівні БД:
* кожен результат має `confidence`; при `confidence < confidence_threshold` (≥ 0.85)
  CHECK-обмеження не дозволяє зберегти `human_review_status = 'not_required'`;
* для задач, що передають текст у LLM, CHECK вимагає `pii_masked = true`;
* жодна таблиця цього модуля не змінює статус документа — це робить лише людина
  через маршрут погодження та КЕП.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import CONFIDENCE_THRESHOLD, Base, Confidence, TimestampMixin, UUIDPkMixin, pg_enum
from ..enums import (
    LLM_TASKS,
    AiRunStatus,
    AiTaskType,
    ComplianceVerdict,
    FindingCategory,
    FindingSeverity,
    FindingStatus,
    HumanReviewStatus,
    ModelProvider,
    RegulatoryLevel,
    RegulatoryStatus,
)
from .documents import SHA256_HEX_CHECK

# Розмірність ембедингів (bge-m3 / multilingual-e5-large — 1024, добре працюють з українською).
EMBEDDING_DIM = 1024

_LLM_TASKS_SQL = "(" + ",".join(f"'{t.value}'" for t in sorted(LLM_TASKS)) + ")"
_REVIEWED_SQL = "('confirmed','corrected','rejected')"


def _hitl_checks(conf_col: str = "confidence") -> tuple[CheckConstraint, ...]:
    """Спільні обмеження Human-in-the-loop для таблиць з оцінкою впевненості."""
    return (
        CheckConstraint(f"{conf_col} IS NULL OR {conf_col} BETWEEN 0 AND 1", name="confidence_range"),
        CheckConstraint(
            f"confidence_threshold >= {CONFIDENCE_THRESHOLD} AND confidence_threshold <= 1",
            name="threshold_not_lowered",
        ),
        CheckConstraint(
            f"NOT coalesce({conf_col} < confidence_threshold, false) "
            "OR human_review_status <> 'not_required'",
            name="low_confidence_needs_review",
        ),
        CheckConstraint(
            f"(human_review_status IN {_REVIEWED_SQL}) = "
            "(reviewed_by_id IS NOT NULL AND reviewed_at IS NOT NULL)",
            name="review_has_reviewer",
        ),
    )


class HumanReviewMixin:
    """Поля ручної валідації оператором."""

    confidence_threshold: Mapped[Decimal] = mapped_column(
        Confidence, server_default=text(str(CONFIDENCE_THRESHOLD))
    )
    human_review_status: Mapped[HumanReviewStatus] = mapped_column(
        pg_enum(HumanReviewStatus, "human_review_status"),
        server_default=HumanReviewStatus.NOT_REQUIRED.value,
    )
    reviewed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    reviewed_at: Mapped[datetime | None]
    review_comment: Mapped[str | None] = mapped_column(Text)


# --- Реєстр моделей і промптів -----------------------------------------------

class AiModel(UUIDPkMixin, TimestampMixin, Base):
    """Конкретна локальна модель (назва + версія + дайджест ваг) для відтворюваності."""

    __tablename__ = "ai_models"

    name: Mapped[str] = mapped_column(String(128))       # напр. «mistral-small-uk-legal»
    version: Mapped[str] = mapped_column(String(64))
    provider: Mapped[ModelProvider] = mapped_column(pg_enum(ModelProvider, "model_provider"))
    task_type: Mapped[AiTaskType] = mapped_column(pg_enum(AiTaskType, "ai_task_type"))
    weights_digest: Mapped[str | None] = mapped_column(String(128))  # sha256 ваг / ollama digest
    quantization: Mapped[str | None] = mapped_column(String(32))
    context_window: Mapped[int | None] = mapped_column(Integer)
    embedding_dim: Mapped[int | None] = mapped_column(SmallInteger)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))

    __table_args__ = (UniqueConstraint("name", "version"),)


class PromptTemplate(UUIDPkMixin, Base):
    """Версіонований промпт; незмінний після створення (нова редакція = нова версія)."""

    __tablename__ = "prompt_templates"

    code: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer)
    task_type: Mapped[AiTaskType] = mapped_column(pg_enum(AiTaskType, "ai_task_type"))
    body: Mapped[str] = mapped_column(Text)
    body_sha256: Mapped[str] = mapped_column(String(64))
    created_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))

    __table_args__ = (
        UniqueConstraint("code", "version"),
        CheckConstraint(f"body_sha256 {SHA256_HEX_CHECK}", name="sha256_format"),
    )


# --- Журнал запусків ----------------------------------------------------------

class AiRun(UUIDPkMixin, HumanReviewMixin, Base):
    """Один виклик AI-пайплайна (Celery/Arq задача). Єдина черга ручної валідації:
    `WHERE human_review_status = 'pending'`.

    Сирий (немаскований) вхід не зберігається — лише його SHA-256 для доказовості.
    """

    __tablename__ = "ai_runs"

    task_type: Mapped[AiTaskType] = mapped_column(pg_enum(AiTaskType, "ai_task_type"))
    status: Mapped[AiRunStatus] = mapped_column(
        pg_enum(AiRunStatus, "ai_run_status"), server_default=AiRunStatus.QUEUED.value
    )
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("documents.id", ondelete="RESTRICT"), index=True
    )
    document_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_versions.id", ondelete="RESTRICT"), index=True
    )
    model_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ai_models.id", ondelete="RESTRICT"))
    prompt_template_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("prompt_templates.id", ondelete="RESTRICT")
    )
    initiated_by_id: Mapped[uuid.UUID | None] = mapped_column(  # NULL — системний тригер
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    worker_task_id: Mapped[str | None] = mapped_column(String(128))

    input_sha256: Mapped[str | None] = mapped_column(String(64))
    pii_masked: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    pii_entities_masked: Mapped[int | None] = mapped_column(Integer)
    parameters: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    output: Mapped[dict[str, Any] | None]  # структурований результат (з маскованими PII)

    confidence: Mapped[Decimal | None] = mapped_column(Confidence)
    requires_human_review: Mapped[bool] = mapped_column(
        Boolean,
        Computed("coalesce(confidence < confidence_threshold, false)", persisted=True),
    )

    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error_message: Mapped[str | None] = mapped_column(Text)
    queued_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]

    __table_args__ = (
        UniqueConstraint("id", "document_version_id", name="uq_ai_runs_id_version"),
        CheckConstraint(f"task_type NOT IN {_LLM_TASKS_SQL} OR pii_masked", name="llm_input_pii_masked"),
        CheckConstraint(
            "status <> 'succeeded' OR (confidence IS NOT NULL AND finished_at IS NOT NULL)",
            name="succeeded_has_confidence",
        ),
        CheckConstraint("status <> 'failed' OR error_message IS NOT NULL", name="failed_has_error"),
        CheckConstraint(f"input_sha256 IS NULL OR input_sha256 {SHA256_HEX_CHECK}", name="sha256_format"),
        CheckConstraint("finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at",
                        name="time_order"),
        *_hitl_checks(),
        Index("ix_ai_runs_review_queue", "queued_at",
              postgresql_where=text("human_review_status = 'pending'")),
        Index("ix_ai_runs_task_status", "task_type", "status"),
    )


class AiExtractedField(UUIDPkMixin, HumanReviewMixin, Base):
    """Реквізит, витягнутий NER/IDP (ПІБ, посада, номер наказу, сума, дата…)."""

    __tablename__ = "ai_extracted_fields"

    ai_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ai_runs.id", ondelete="RESTRICT"), index=True)
    document_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="RESTRICT"), index=True
    )
    field_key: Mapped[str] = mapped_column(String(64))   # ключ з DocumentType.requisites_schema
    extracted_value: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(
        JSONB
    )
    validated_value: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(
        JSONB
    )
    confidence: Mapped[Decimal] = mapped_column(Confidence)
    requires_human_review: Mapped[bool] = mapped_column(
        Boolean, Computed("confidence < confidence_threshold", persisted=True)
    )
    page_no: Mapped[int | None] = mapped_column(SmallInteger)
    bbox: Mapped[list[Any] | None]  # [x0, y0, x1, y1] у координатах сторінки
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))

    __table_args__ = (
        UniqueConstraint("ai_run_id", "field_key"),
        CheckConstraint(
            "human_review_status <> 'corrected' OR validated_value IS NOT NULL",
            name="corrected_has_value",
        ),
        *_hitl_checks(),
    )


# --- Нормативний корпус для RAG ----------------------------------------------

class RegulatorySource(UUIDPkMixin, TimestampMixin, Base):
    """Нормативний акт: Статут ПУЕТ, положення, накази МОН, закони, ДСТУ."""

    __tablename__ = "regulatory_sources"

    level: Mapped[RegulatoryLevel] = mapped_column(pg_enum(RegulatoryLevel, "regulatory_level"))
    issuer: Mapped[str] = mapped_column(String(256))        # «Вчена рада ПУЕТ», «МОН України»
    title: Mapped[str] = mapped_column(String(1024))
    doc_number: Mapped[str | None] = mapped_column(String(64))
    adopted_on: Mapped[date | None]
    effective_from: Mapped[date | None]
    effective_to: Mapped[date | None]
    status: Mapped[RegulatoryStatus] = mapped_column(pg_enum(RegulatoryStatus, "regulatory_status"))
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("regulatory_sources.id", ondelete="RESTRICT")
    )
    # Внутрішній акт ПУЕТ може бути документом цієї ж СЕД.
    document_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("documents.id", ondelete="RESTRICT"))
    org_unit_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("org_units.id", ondelete="RESTRICT"))
    source_url: Mapped[str | None] = mapped_column(String(2048))
    storage_bucket: Mapped[str | None] = mapped_column(String(63))
    storage_key: Mapped[str | None] = mapped_column(String(1024))
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    current_index_revision: Mapped[int] = mapped_column(Integer, server_default=text("0"))

    __table_args__ = (
        CheckConstraint("effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from",
                        name="effective_period"),
        CheckConstraint("status <> 'repealed' OR effective_to IS NOT NULL", name="repealed_has_end"),
        CheckConstraint("supersedes_id IS NULL OR supersedes_id <> id", name="not_self_supersede"),
        CheckConstraint(f"content_sha256 IS NULL OR content_sha256 {SHA256_HEX_CHECK}", name="sha256_format"),
        Index("ix_regulatory_sources_in_force", "level", "effective_from", "effective_to"),
    )


class RegulatoryChunk(UUIDPkMixin, Base):
    """Фрагмент акта (пункт/підпункт) з dense- та sparse-представленням.

    Фрагменти незмінні: переіндексація створює нову `index_revision`, старі
    лишаються, щоб цитати в історичних перевірках не «зависали».
    """

    __tablename__ = "regulatory_chunks"

    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("regulatory_sources.id", ondelete="RESTRICT"), index=True
    )
    index_revision: Mapped[int] = mapped_column(Integer)
    chunk_no: Mapped[int] = mapped_column(Integer)
    section_ref: Mapped[str | None] = mapped_column(String(256))  # «розд. IV, п. 4.3»
    content: Mapped[str] = mapped_column(Text)
    content_tsv: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', content)", persisted=True)
    )
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    embedding_model_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ai_models.id", ondelete="RESTRICT")
    )
    token_count: Mapped[int | None] = mapped_column(Integer)
    is_current: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))

    __table_args__ = (
        UniqueConstraint("source_id", "index_revision", "chunk_no"),
        CheckConstraint("(embedding IS NULL) = (embedding_model_id IS NULL)", name="embedding_has_model"),
        Index("ix_regulatory_chunks_content_tsv", "content_tsv", postgresql_using="gin"),
        Index(
            "ix_regulatory_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 128},
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_where=text("is_current"),
        ),
    )


# --- Комплаєнс-перевірки ------------------------------------------------------

class ComplianceCheck(UUIDPkMixin, Base):
    """Результат комплаєнс-аудиту версії документа (1:1 з AiRun).

    Ручна валідація, впевненість і модель — в `ai_runs`; тут доменний результат.
    """

    __tablename__ = "compliance_checks"

    ai_run_id: Mapped[uuid.UUID] = mapped_column(unique=True)
    document_version_id: Mapped[uuid.UUID] = mapped_column(index=True)
    verdict: Mapped[ComplianceVerdict] = mapped_column(pg_enum(ComplianceVerdict, "compliance_verdict"))
    summary: Mapped[str | None] = mapped_column(Text)
    # Дата, на яку оцінюється чинність норм (зазвичай дата документа).
    regulations_as_of: Mapped[date]
    retrieval_config: Mapped[dict[str, Any]] = mapped_column(
        server_default=text("'{}'::jsonb")  # dense_k, bm25_k, rrf_k, reranker…
    )
    sources_considered: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))

    findings: Mapped[list[ComplianceFinding]] = relationship(
        back_populates="check", order_by="ComplianceFinding.ordinal", lazy="raise"
    )

    __table_args__ = (
        # Перевірка стосується тієї ж версії, що й запуск ШІ.
        ForeignKeyConstraint(
            ["ai_run_id", "document_version_id"],
            ["ai_runs.id", "ai_runs.document_version_id"],
            name="fk_compliance_checks_run_same_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"],
            name="fk_compliance_checks_document_version", ondelete="RESTRICT",
        ),
    )


class ComplianceFinding(UUIDPkMixin, Base):
    """Окреме зауваження: суперечність, застаріле посилання, відсутній реквізит…"""

    __tablename__ = "compliance_findings"

    check_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("compliance_checks.id", ondelete="RESTRICT"), index=True
    )
    ordinal: Mapped[int] = mapped_column(SmallInteger)
    severity: Mapped[FindingSeverity] = mapped_column(pg_enum(FindingSeverity, "finding_severity"))
    category: Mapped[FindingCategory] = mapped_column(pg_enum(FindingCategory, "finding_category"))
    title: Mapped[str] = mapped_column(String(512))
    explanation: Mapped[str] = mapped_column(Text)
    recommendation: Mapped[str | None] = mapped_column(Text)
    # Фрагмент документа, до якого зауваження (зміщення в extracted_text версії).
    fragment_text: Mapped[str | None] = mapped_column(Text)
    page_no: Mapped[int | None] = mapped_column(SmallInteger)
    char_start: Mapped[int | None] = mapped_column(Integer)
    char_end: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[Decimal] = mapped_column(Confidence)
    requires_human_review: Mapped[bool] = mapped_column(
        Boolean, Computed(f"confidence < {CONFIDENCE_THRESHOLD}", persisted=True)
    )
    status: Mapped[FindingStatus] = mapped_column(
        pg_enum(FindingStatus, "finding_status"), server_default=FindingStatus.OPEN.value
    )
    resolved_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    resolved_at: Mapped[datetime | None]
    resolution_comment: Mapped[str | None] = mapped_column(Text)

    check: Mapped[ComplianceCheck] = relationship(back_populates="findings", lazy="raise")
    citations: Mapped[list[ComplianceCitation]] = relationship(
        back_populates="finding", order_by="ComplianceCitation.rank", lazy="raise"
    )

    __table_args__ = (
        UniqueConstraint("check_id", "ordinal"),
        CheckConstraint("confidence BETWEEN 0 AND 1", name="confidence_range"),
        CheckConstraint(
            "(char_start IS NULL AND char_end IS NULL) OR (char_start >= 0 AND char_end > char_start)",
            name="fragment_span",
        ),
        CheckConstraint(
            "(status = 'open') = (resolved_by_id IS NULL AND resolved_at IS NULL)",
            name="resolution_has_actor",
        ),
        CheckConstraint(
            "status <> 'dismissed' OR length(btrim(coalesce(resolution_comment, ''))) > 0",
            name="dismissal_has_reason",
        ),
        Index("ix_compliance_findings_open", "severity", postgresql_where=text("status = 'open'")),
    )


class ComplianceCitation(UUIDPkMixin, Base):
    """Цитата-першоджерело. `quoted_text` — знімок на момент перевірки;
    `quote_verified` — пайплайн підтвердив, що цитата буквально є у фрагменті
    (захист від галюцинованих цитат).
    """

    __tablename__ = "compliance_citations"

    finding_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("compliance_findings.id", ondelete="RESTRICT"), index=True
    )
    chunk_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("regulatory_chunks.id", ondelete="RESTRICT"), index=True
    )
    rank: Mapped[int] = mapped_column(SmallInteger)
    quoted_text: Mapped[str] = mapped_column(Text)
    quote_verified: Mapped[bool] = mapped_column(Boolean)
    source_status_snapshot: Mapped[RegulatoryStatus] = mapped_column(
        pg_enum(RegulatoryStatus, "regulatory_status")
    )
    dense_score: Mapped[float | None]
    sparse_score: Mapped[float | None]
    fused_score: Mapped[float | None]
    rerank_score: Mapped[float | None]

    finding: Mapped[ComplianceFinding] = relationship(back_populates="citations", lazy="raise")

    __table_args__ = (
        UniqueConstraint("finding_id", "rank"),
        UniqueConstraint("finding_id", "chunk_id"),
        CheckConstraint("rank > 0", name="rank_positive"),
        CheckConstraint("length(quoted_text) > 0", name="quote_not_empty"),
    )
