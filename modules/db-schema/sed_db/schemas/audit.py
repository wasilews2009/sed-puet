"""Читання audit_logs, у тому числі типізовані результати аналізу ШІ."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from ..enums import AiTaskType, AuditActorType, AuditEventCategory, FindingCategory, FindingSeverity
from .common import ReadModel


class AiViolationCitation(BaseModel):
    source: str
    doc_number: str | None = None
    section: str | None = None
    quote: str
    quote_verified: bool


class AiViolation(BaseModel):
    """Виявлене порушення у знімку audit_logs.violations."""

    finding_id: uuid.UUID
    severity: FindingSeverity
    category: FindingCategory
    title: str
    confidence: Decimal
    requires_human_review: bool
    char_start: int | None = None
    char_end: int | None = None
    citations: list[AiViolationCitation] = Field(default_factory=list)


class ExtractedMetadataEntry(BaseModel):
    """Реквізит у знімку audit_logs.extracted_metadata. ПД — лише як `masked` + посилання."""

    value: Any = None
    masked: bool = False
    field_id: uuid.UUID | None = None
    confidence: Decimal
    requires_human_review: bool


class AuditLogRead(ReadModel):
    id: int
    chain_seq: int
    occurred_at: datetime
    category: AuditEventCategory
    actor_type: AuditActorType
    actor_user_id: uuid.UUID | None
    action: str
    entity_table: str
    entity_id: uuid.UUID | None
    document_id: uuid.UUID | None
    document_version_id: uuid.UUID | None
    changes: dict[str, Any]
    request_id: str | None


class AiAnalysisAuditRead(AuditLogRead):
    """Запис category = 'ai_analysis': що саме вирішила модель і з якою впевненістю."""

    ai_run_id: uuid.UUID
    ai_task_type: AiTaskType
    ai_model: str | None
    confidence_score: Decimal
    requires_human_review: bool
    ai_verdict: str | None
    violations: list[AiViolation]
    extracted_metadata: dict[str, ExtractedMetadataEntry]


class AuditChainVerification(BaseModel):
    ok: bool
    broken_seq: int | None = None
    reason: str | None = None
