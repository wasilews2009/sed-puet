"""Журнал audit_logs: append-only, з хеш-ланцюжком, у тому числі результати аналізу ШІ."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    LargeBinary,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base, Confidence, pg_enum
from ..enums import AiTaskType, AuditActorType, AuditEventCategory


class AuditLog(Base):
    """Єдиний журнал дій СЕД. Три категорії записів:

    * `data_change` — зміна ключових таблиць (тригер `sed_audit_row_change()`);
    * `ai_analysis` — підсумок завершеного запуску ШІ: `confidence_score`,
      `violations` (виявлені порушення з цитатами), `extracted_metadata`
      (вилучені реквізити). Пише відкладений тригер на COMMIT транзакції,
      у якій запуск отримав статус `succeeded`, тож усі зауваження вже записані;
    * `business_event` — перегляд, експорт, завантаження (функція `sed_audit_event()`).

    `chain_seq`, `prev_hash`, `record_hash` заповнює тригер під advisory-lock;
    UPDATE/DELETE/TRUNCATE заборонені. `entity_id`/`document_id` навмисно без FK:
    журнал має переживати будь-які зміни в предметних таблицях.

    Персональні дані: реквізити, позначені в `requisites_schema` як `"x-pii": true`,
    потрапляють у `extracted_metadata` лише замаскованими (значення лишається
    в `ai_extracted_fields`, яке підпадає під строки зберігання й контроль доступу).
    """

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    chain_seq: Mapped[int] = mapped_column(BigInteger, unique=True, server_default=text("0"))
    occurred_at: Mapped[datetime] = mapped_column(server_default=text("clock_timestamp()"))
    category: Mapped[AuditEventCategory] = mapped_column(
        pg_enum(AuditEventCategory, "audit_event_category"),
        server_default=AuditEventCategory.DATA_CHANGE.value,
    )
    actor_type: Mapped[AuditActorType] = mapped_column(pg_enum(AuditActorType, "audit_actor_type"))
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    action: Mapped[str] = mapped_column(String(64))  # 'insert' | 'update' | 'ai.compliance_check' …
    entity_table: Mapped[str] = mapped_column(String(63))
    entity_id: Mapped[uuid.UUID | None]
    document_id: Mapped[uuid.UUID | None]
    document_version_id: Mapped[uuid.UUID | None]
    changes: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))

    # --- результати аналізу ШІ (лише category = 'ai_analysis') ---
    ai_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ai_runs.id", ondelete="RESTRICT"))
    ai_task_type: Mapped[AiTaskType | None] = mapped_column(pg_enum(AiTaskType, "ai_task_type"))
    ai_model: Mapped[str | None] = mapped_column(String(200))  # «назва:версія» — знімок
    confidence_score: Mapped[Decimal | None] = mapped_column(Confidence)
    requires_human_review: Mapped[bool | None] = mapped_column(Boolean)
    ai_verdict: Mapped[str | None] = mapped_column(String(64))  # вердикт комплаєнсу / клас документа
    violations: Mapped[list[Any] | None] = mapped_column(JSONB)
    extracted_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    request_id: Mapped[str | None] = mapped_column(String(64))
    client_ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(String(512))
    prev_hash: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    record_hash: Mapped[bytes] = mapped_column(LargeBinary(32), server_default=text("'\\x'::bytea"))

    __table_args__ = (
        CheckConstraint(
            "(category = 'ai_analysis') = (ai_run_id IS NOT NULL)", name="ai_fields_only_for_ai"
        ),
        CheckConstraint(
            "category <> 'ai_analysis' OR (ai_task_type IS NOT NULL AND confidence_score IS NOT NULL "
            "AND requires_human_review IS NOT NULL AND jsonb_typeof(violations) = 'array' "
            "AND jsonb_typeof(extracted_metadata) = 'object')",
            name="ai_analysis_complete",
        ),
        CheckConstraint(
            "confidence_score IS NULL OR confidence_score BETWEEN 0 AND 1", name="confidence_range"
        ),
        # Один підсумковий запис на запуск ШІ.
        Index("uq_audit_logs_ai_run", "ai_run_id", unique=True,
              postgresql_where=text("ai_run_id IS NOT NULL")),
        Index("ix_audit_logs_document", "document_id", "occurred_at"),
        Index("ix_audit_logs_entity", "entity_table", "entity_id"),
        Index("ix_audit_logs_ai_review", "occurred_at",
              postgresql_where=text("category = 'ai_analysis' AND requires_human_review")),
        Index("ix_audit_logs_violations", "violations", postgresql_using="gin",
              postgresql_ops={"violations": "jsonb_path_ops"}),
        Index("ix_audit_logs_occurred_brin", "occurred_at", postgresql_using="brin"),
    )
