"""Документи, версії та історія статусів."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import Field, model_validator

from ..enums import AccessLevel, AuditActorType, DocumentDirection, DocumentStatus, VersionSource
from .common import InputModel, NonEmptyStr, ReadModel, Sha256Hex

S = DocumentStatus

# Дозволені переходи життєвого циклу (БД додатково блокує «ШІ → юридичні статуси»
# і «підписано без валідного КЕП»; registered ставить лише sed_register_document()).
ALLOWED_TRANSITIONS: dict[DocumentStatus, frozenset[DocumentStatus]] = {
    S.DRAFT: frozenset({S.AI_REVIEW, S.ON_APPROVAL, S.CANCELLED}),
    S.AI_REVIEW: frozenset({S.DRAFT}),
    S.ON_APPROVAL: frozenset({S.APPROVED, S.REVISION_REQUIRED, S.CANCELLED}),
    S.REVISION_REQUIRED: frozenset({S.DRAFT, S.CANCELLED}),
    S.APPROVED: frozenset({S.SIGNED, S.REVISION_REQUIRED}),
    S.SIGNED: frozenset(),
    S.REGISTERED: frozenset({S.IN_EXECUTION, S.ARCHIVED}),
    S.IN_EXECUTION: frozenset({S.EXECUTED}),
    S.EXECUTED: frozenset({S.ARCHIVED}),
    S.ARCHIVED: frozenset(),
    S.CANCELLED: frozenset(),
}


def check_transition(current: DocumentStatus, target: DocumentStatus) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"Перехід «{current.value}» → «{target.value}» не дозволено")


class DocumentCreate(InputModel):
    document_type_id: uuid.UUID
    direction: DocumentDirection = DocumentDirection.INTERNAL
    access_level: AccessLevel = AccessLevel.INTERNAL
    title: NonEmptyStr = Field(max_length=1024, description="Заголовок до тексту")
    summary: str | None = None
    org_unit_id: uuid.UUID
    nomenclature_case_id: uuid.UUID | None = None
    due_date: date | None = None
    correspondent_name: str | None = Field(default=None, max_length=512)
    correspondent_number: str | None = Field(default=None, max_length=64)
    correspondent_date: date | None = None

    @model_validator(mode="after")
    def _incoming(self) -> DocumentCreate:
        if self.direction is DocumentDirection.INCOMING and not self.correspondent_name:
            raise ValueError("Для вхідного документа обов'язковий кореспондент")
        return self


class DocumentUpdate(InputModel):
    """Часткове оновлення картки. `row_version` — для оптимістичного блокування."""

    row_version: int = Field(ge=1)
    title: NonEmptyStr | None = Field(default=None, max_length=1024)
    summary: str | None = None
    access_level: AccessLevel | None = None
    nomenclature_case_id: uuid.UUID | None = None
    due_date: date | None = None
    requisites: dict[str, Any] | None = Field(default=None, description="Реквізити після валідації людиною")


class StatusTransitionRequest(InputModel):
    to_status: DocumentStatus
    reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _reason(self) -> StatusTransitionRequest:
        if self.to_status in {S.CANCELLED, S.REVISION_REQUIRED} and not self.reason:
            raise ValueError("Для скасування або повернення на доопрацювання потрібна причина")
        if self.to_status is S.REGISTERED:
            raise ValueError("Реєстрація виконується окремою дією з присвоєнням номера")
        return self


class DocumentVersionCreate(InputModel):
    """Метадані нової версії; сам файл уже завантажено в MinIO за presigned URL."""

    source: VersionSource
    storage_bucket: str = Field(max_length=63)
    storage_key: str = Field(max_length=1024)
    storage_version_id: str | None = Field(default=None, max_length=128)
    file_name: NonEmptyStr = Field(max_length=512)
    mime_type: str = Field(max_length=128, pattern=r"^[\w.+-]+/[\w.+-]+$")
    size_bytes: int = Field(gt=0, le=200 * 1024 * 1024)
    content_sha256: Sha256Hex
    page_count: int | None = Field(default=None, ge=1)
    parent_version_id: uuid.UUID | None = None
    change_note: str | None = Field(default=None, max_length=1024)


class DocumentVersionRead(ReadModel):
    id: uuid.UUID
    document_id: uuid.UUID
    version_number: int
    parent_version_id: uuid.UUID | None
    source: VersionSource
    file_name: str
    mime_type: str
    size_bytes: int
    content_sha256: str
    page_count: int | None
    change_note: str | None
    created_by_id: uuid.UUID
    created_at: datetime
    is_locked: bool
    locked_at: datetime | None


class DocumentRead(ReadModel):
    id: uuid.UUID
    document_type_id: uuid.UUID
    direction: DocumentDirection
    status: DocumentStatus
    access_level: AccessLevel
    title: str
    summary: str | None
    author_id: uuid.UUID
    org_unit_id: uuid.UUID
    nomenclature_case_id: uuid.UUID | None
    registration_number: str | None
    registration_date: date | None
    correspondent_name: str | None
    due_date: date | None
    effective_from: date | None
    requisites: dict[str, Any]
    current_version_id: uuid.UUID | None
    row_version: int
    created_at: datetime
    updated_at: datetime


class DocumentStatusHistoryRead(ReadModel):
    id: int
    document_id: uuid.UUID
    from_status: DocumentStatus | None
    to_status: DocumentStatus
    document_version_id: uuid.UUID | None
    route_id: uuid.UUID | None
    actor_type: AuditActorType
    changed_by_id: uuid.UUID | None
    reason: str | None
    changed_at: datetime
