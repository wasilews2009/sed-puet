"""DTO API застосунку (Pydantic v2). Модулі інжесту, комплаєнсу і маршрутів мають власні DTO."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from sed_db.base import CONFIDENCE_THRESHOLD
from sed_db.enums import (
    AccessLevel,
    AiRunStatus,
    ComplianceVerdict,
    DocumentDirection,
    DocumentStatus,
    FindingCategory,
    FindingSeverity,
    FindingStatus,
    HumanReviewStatus,
    OrgUnitKind,
    RegulatoryStatus,
    RouteStatus,
    SignatureFormat,
    SignatureProvider,
    SignatureValidation,
    StepKind,
    StepStatus,
    UserRole,
    VersionSource,
)
from sed_db.schemas.common import InputModel


# --- Користувач і довідники -------------------------------------------------------------

class RoleOut(BaseModel):
    role: UserRole
    role_label: str
    org_unit_id: uuid.UUID | None
    org_unit_name: str | None
    scope_note: str | None
    is_acting: bool


class Permissions(BaseModel):
    upload_all_units: bool = Field(description="Ректор/проректор: реєстрація документів будь-якого підрозділу")
    upload_unit_ids: list[uuid.UUID]
    corpus_admin: bool = Field(description="Може змінювати нормативний корпус (RAG)")
    view_all_documents: bool


class MeOut(BaseModel):
    id: uuid.UUID
    display_name: str
    email: str
    primary_org_unit_id: uuid.UUID | None
    roles: list[RoleOut]
    permissions: Permissions


class OrgUnitOut(BaseModel):
    id: uuid.UUID
    parent_id: uuid.UUID | None
    kind: OrgUnitKind
    code: str
    name: str
    short_name: str | None


class DocumentTypeOut(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    requires_kep: bool


class ModulesOut(BaseModel):
    ingest: bool
    compliance: bool
    routing: bool
    kep_mock: bool = Field(description="Імітаційний КНЕДП (лише dev/test): підпис тестовим ключем без юридичної сили")


class DictionariesOut(BaseModel):
    org_units: list[OrgUnitOut]
    document_types: list[DocumentTypeOut]
    labels: dict[str, dict[str, str]]
    field_labels: dict[str, str]
    confidence_threshold: Decimal = CONFIDENCE_THRESHOLD
    modules: ModulesOut


class AuthConfigOut(BaseModel):
    mode: Literal["keycloak", "dev"]
    issuer: str | None = None
    client_id: str | None = None
    environment: str


class DevUserOut(BaseModel):
    id: uuid.UUID
    display_name: str
    email: str
    roles: list[str]


class DevLoginIn(InputModel):
    user_id: uuid.UUID


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int


# --- Список документів -----------------------------------------------------------------

class DocumentSort(StrEnum):
    UPDATED_DESC = "-updated_at"
    CREATED_DESC = "-created_at"
    CREATED_ASC = "created_at"
    TITLE = "title"


class DocumentListItem(BaseModel):
    id: uuid.UUID
    title: str
    document_type_code: str
    document_type_name: str
    status: DocumentStatus
    direction: DocumentDirection
    access_level: AccessLevel
    org_unit_id: uuid.UUID
    org_unit_name: str
    author_name: str
    registration_number: str | None
    registration_date: date | None
    created_at: datetime
    updated_at: datetime
    ai_confidence: Decimal | None = Field(description="Впевненість вилучення реквізитів поточної версії")
    needs_review: bool = Field(description="Є реквізити або перевірки ШІ, що чекають ручної валідації")
    open_findings: int = Field(description="Відкриті суттєві/критичні зауваження комплаєнсу")
    compliance_verdict: ComplianceVerdict | None


class DocumentPage(BaseModel):
    items: list[DocumentListItem]
    total: int
    limit: int
    offset: int


# --- Картка документа ---------------------------------------------------------------------

class PersonRef(BaseModel):
    id: uuid.UUID
    name: str


class VersionOut(BaseModel):
    id: uuid.UUID
    version_number: int
    source: VersionSource
    file_name: str
    mime_type: str
    size_bytes: int
    content_sha256: str
    page_count: int | None
    created_at: datetime
    created_by: PersonRef
    is_locked: bool
    change_note: str | None
    has_text: bool


class ExtractedFieldOut(BaseModel):
    id: uuid.UUID
    key: str
    label: str
    extracted_value: Any
    validated_value: Any
    confidence: Decimal
    requires_human_review: bool
    review_status: HumanReviewStatus
    reviewed_by: PersonRef | None
    reviewed_at: datetime | None
    review_comment: str | None
    issues: list[str]
    page_no: int | None


class AiRunOut(BaseModel):
    id: uuid.UUID
    status: AiRunStatus
    model: str
    confidence: Decimal | None
    requires_human_review: bool
    review_status: HumanReviewStatus
    error_message: str | None
    finished_at: datetime | None
    warnings: list[str] = Field(default_factory=list)


class ExtractionOut(BaseModel):
    run: AiRunOut
    fields: list[ExtractedFieldOut]
    ocr: dict[str, Any] | None = Field(None, description="Сторінки OCR: джерело тексту і якість")
    ocr_confidence: Decimal | None


class CitationOut(BaseModel):
    rank: int
    citation_label: str
    source_title: str
    section_ref: str | None
    quoted_text: str
    quote_verified: bool
    source_status: RegulatoryStatus


class FindingOut(BaseModel):
    id: uuid.UUID
    ordinal: int
    severity: FindingSeverity
    category: FindingCategory
    title: str
    explanation: str
    recommendation: str | None
    fragment_text: str | None
    confidence: Decimal
    requires_human_review: bool
    status: FindingStatus
    resolved_by: PersonRef | None
    resolved_at: datetime | None
    resolution_comment: str | None
    citations: list[CitationOut]


class ComplianceOut(BaseModel):
    check_id: uuid.UUID
    run: AiRunOut
    document_version_id: uuid.UUID
    verdict: ComplianceVerdict
    summary: str | None
    regulations_as_of: date
    sources_considered: int
    created_at: datetime
    review_reasons: list[str]
    findings: list[FindingOut]


class SignatureOut(BaseModel):
    id: uuid.UUID
    document_version_id: uuid.UUID
    approval_step_id: uuid.UUID | None
    signer: PersonRef
    signature_format: SignatureFormat
    provider: SignatureProvider
    cert_subject_cn: str
    cert_issuer: str
    cert_serial: str
    signed_at: datetime
    validation_status: SignatureValidation
    validated_at: datetime | None


class StepOut(BaseModel):
    id: uuid.UUID
    stage_no: int
    ordinal: int
    step_kind: StepKind
    assignee: PersonRef
    original_assignee: PersonRef | None
    acting_role: UserRole | None
    status: StepStatus
    is_mandatory: bool
    requires_kep: bool
    activated_at: datetime | None
    due_at: datetime | None
    decided_at: datetime | None
    decision_comment: str | None
    is_overdue: bool


class RouteOut(BaseModel):
    id: uuid.UUID
    status: RouteStatus
    document_version_id: uuid.UUID
    template_name: str | None
    current_stage: int | None
    initiated_by: PersonRef
    started_at: datetime | None
    completed_at: datetime | None
    cancel_reason: str | None
    steps: list[StepOut]


class StatusChangeOut(BaseModel):
    from_status: DocumentStatus | None
    to_status: DocumentStatus
    changed_by: PersonRef | None
    actor_type: str
    reason: str | None
    changed_at: datetime


class CardPermissions(BaseModel):
    review: bool = Field(description="Валідувати реквізити ШІ і розглядати зауваження комплаєнсу")
    check_compliance: bool
    start_route: bool
    my_active_step_id: uuid.UUID | None = Field(description="Мій активний крок погодження за цим документом")


class DocumentCard(BaseModel):
    id: uuid.UUID
    title: str
    summary: str | None
    document_type: DocumentTypeOut
    status: DocumentStatus
    direction: DocumentDirection
    access_level: AccessLevel
    org_unit: OrgUnitOut
    author: PersonRef
    registration_number: str | None
    registration_date: date | None
    correspondent_name: str | None
    correspondent_number: str | None
    correspondent_date: date | None
    due_date: date | None
    requisites: dict[str, Any] = Field(description="Реквізити, підтверджені людиною")
    created_at: datetime
    updated_at: datetime
    row_version: int
    current_version_id: uuid.UUID | None
    versions: list[VersionOut]
    extraction: ExtractionOut | None
    compliance: ComplianceOut | None
    route: RouteOut | None
    signatures: list[SignatureOut]
    history: list[StatusChangeOut]
    permissions: CardPermissions
    disclaimer: str = ("Реквізити і зауваження сформовано ШІ-асистентом; юридичної сили вони не мають. "
                       "Рішення ухвалює посадова особа, накладаючи КЕП.")


# --- Ручна валідація (Human-in-the-loop) -------------------------------------------------

class FieldDecision(StrEnum):
    CONFIRM = "confirm"
    CORRECT = "correct"
    REJECT = "reject"


class FieldReviewIn(InputModel):
    decision: FieldDecision
    value: Any = Field(None, description="Нове значення для `correct` (рядок, дата YYYY-MM-DD, об'єкт особи…)")
    comment: str | None = Field(None, max_length=2000)
    row_version: int | None = Field(None, description="Версія картки, яку бачив оператор (оптимістичне блокування)")

    @model_validator(mode="after")
    def _value(self) -> FieldReviewIn:
        if self.decision is FieldDecision.CORRECT and self.value in (None, "", [], {}):
            raise ValueError("Для виправлення потрібне нове значення")
        if self.decision is FieldDecision.REJECT and not (self.comment and self.comment.strip()):
            raise ValueError("Вкажіть, чому значення відхилено")
        return self


class FindingResolutionIn(InputModel):
    status: Literal[FindingStatus.ACCEPTED, FindingStatus.DISMISSED, FindingStatus.FIXED]
    comment: str | None = Field(None, max_length=4000)

    @model_validator(mode="after")
    def _reason(self) -> FindingResolutionIn:
        if self.status is FindingStatus.DISMISSED and not (self.comment and self.comment.strip()):
            raise ValueError("Поясніть, чому зауваження хибне")
        return self


# --- Черги ------------------------------------------------------------------------------------

class InboxItem(BaseModel):
    step_id: uuid.UUID
    route_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    document_type_name: str
    document_status: DocumentStatus
    org_unit_name: str
    author_name: str
    step_kind: StepKind
    stage_no: int
    acting_role: UserRole | None
    requires_kep: bool
    activated_at: datetime | None
    due_at: datetime | None
    is_overdue: bool
    delegated_from: str | None


class ReviewQueueItem(BaseModel):
    document_id: uuid.UUID
    document_title: str
    org_unit_name: str
    task: Literal["requisites", "compliance"]
    ai_run_id: uuid.UUID
    confidence: Decimal | None
    pending_fields: int
    queued_at: datetime
