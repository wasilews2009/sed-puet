"""Запуски ШІ, вилучені реквізити, комплаєнс-перевірки та ручна валідація."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import Field, computed_field, model_validator

from ..base import CONFIDENCE_THRESHOLD
from ..enums import (
    LLM_TASKS,
    AiRunStatus,
    AiTaskType,
    ComplianceVerdict,
    FindingCategory,
    FindingSeverity,
    FindingStatus,
    HumanReviewStatus,
)
from .common import Confidence, InputModel, NonEmptyStr, ReadModel, Sha256Hex, needs_human_review


class AiRunCreate(InputModel):
    task_type: AiTaskType
    model_id: uuid.UUID
    prompt_template_id: uuid.UUID | None = None
    document_version_id: uuid.UUID
    worker_task_id: str | None = Field(default=None, max_length=128)
    pii_masked: bool = Field(description="Вхід пройшов Presidio/spaCy-маскування до передачі в модель")
    parameters: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _pii(self) -> AiRunCreate:
        if self.task_type in LLM_TASKS and not self.pii_masked:
            raise ValueError("Текст для LLM має бути анонімізований (pii_masked = true)")
        return self


class ExtractedFieldIn(InputModel):
    field_key: str = Field(max_length=64, pattern=r"^[a-z][a-z0-9_]*$",
                           examples=["order_number", "signer_full_name", "amount_uah", "order_date"])
    extracted_value: Any = None
    confidence: Confidence
    page_no: int | None = Field(default=None, ge=1)
    bbox: tuple[float, float, float, float] | None = None


class CitationIn(InputModel):
    chunk_id: uuid.UUID
    quoted_text: NonEmptyStr
    quote_verified: bool = Field(description="Цитату буквально знайдено у фрагменті акта")
    dense_score: float | None = None
    sparse_score: float | None = None
    fused_score: float | None = None
    rerank_score: float | None = None


class FindingIn(InputModel):
    severity: FindingSeverity
    category: FindingCategory
    title: NonEmptyStr = Field(max_length=512)
    explanation: NonEmptyStr
    recommendation: str | None = None
    fragment_text: str | None = None
    page_no: int | None = Field(default=None, ge=1)
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=1)
    confidence: Confidence
    citations: list[CitationIn] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def _checks(self) -> FindingIn:
        if (self.char_start is None) != (self.char_end is None):
            raise ValueError("char_start і char_end задаються разом")
        if self.char_start is not None and self.char_end is not None and self.char_end <= self.char_start:
            raise ValueError("char_end має бути більшим за char_start")
        if not any(c.quote_verified for c in self.citations):
            raise ValueError("Зауваження потребує хоча б однієї перевіреної цитати-першоджерела")
        if len({c.chunk_id for c in self.citations}) != len(self.citations):
            raise ValueError("Один фрагмент не цитується двічі в межах зауваження")
        return self


class ComplianceCheckIn(InputModel):
    verdict: ComplianceVerdict
    summary: str | None = None
    regulations_as_of: date
    retrieval_config: dict[str, Any] = Field(default_factory=dict)
    sources_considered: int = Field(default=0, ge=0)
    findings: list[FindingIn] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def _verdict(self) -> ComplianceCheckIn:
        if self.verdict is ComplianceVerdict.COMPLIANT and any(
            f.severity in {FindingSeverity.MAJOR, FindingSeverity.CRITICAL} for f in self.findings
        ):
            raise ValueError("Вердикт «compliant» несумісний із суттєвими зауваженнями")
        if self.verdict in {ComplianceVerdict.ISSUES_FOUND, ComplianceVerdict.NON_COMPLIANT} and not self.findings:
            raise ValueError("Вердикт з порушеннями потребує хоча б одного зауваження")
        return self


class AiRunResult(InputModel):
    """Завершення запуску. Реквізити й перевірка пишуться в тій самій транзакції,
    тому підсумковий запис audit_logs (на COMMIT) містить усі порушення й метадані."""

    status: Literal[AiRunStatus.SUCCEEDED, AiRunStatus.FAILED, AiRunStatus.CANCELLED]
    confidence: Confidence | None = None
    input_sha256: Sha256Hex | None = None
    pii_entities_masked: int | None = Field(default=None, ge=0)
    output: dict[str, Any] | None = None
    tokens_in: int | None = Field(default=None, ge=0)
    tokens_out: int | None = Field(default=None, ge=0)
    latency_ms: int | None = Field(default=None, ge=0)
    error_message: str | None = None
    extracted_fields: list[ExtractedFieldIn] = Field(default_factory=list, max_length=200)
    compliance_check: ComplianceCheckIn | None = None

    @model_validator(mode="after")
    def _status(self) -> AiRunResult:
        if self.status is AiRunStatus.SUCCEEDED and self.confidence is None:
            raise ValueError("Успішний запуск має містити confidence")
        if self.status is AiRunStatus.FAILED and not self.error_message:
            raise ValueError("Невдалий запуск має містити error_message")
        if self.status is not AiRunStatus.SUCCEEDED and (self.extracted_fields or self.compliance_check):
            raise ValueError("Результати аналізу приймаються лише для успішного запуску")
        keys = [f.field_key for f in self.extracted_fields]
        if len(keys) != len(set(keys)):
            raise ValueError("field_key реквізитів мають бути унікальними")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def requires_human_review(self) -> bool:
        return needs_human_review(self.confidence) or any(
            needs_human_review(f.confidence) for f in self.extracted_fields
        )


class HumanReviewDecision(InputModel):
    """Рішення оператора по запуску або окремому реквізиту."""

    status: Literal[HumanReviewStatus.CONFIRMED, HumanReviewStatus.CORRECTED, HumanReviewStatus.REJECTED]
    comment: str | None = Field(default=None, max_length=5000)
    corrected_value: Any = None

    @model_validator(mode="after")
    def _corrected(self) -> HumanReviewDecision:
        if self.status is HumanReviewStatus.CORRECTED and self.corrected_value is None:
            raise ValueError("Для «виправлено» потрібне corrected_value")
        if self.status is HumanReviewStatus.REJECTED and not self.comment:
            raise ValueError("Відхилення результату ШІ потребує коментаря")
        return self


class FindingResolution(InputModel):
    status: Literal[FindingStatus.ACCEPTED, FindingStatus.DISMISSED, FindingStatus.FIXED]
    comment: str | None = Field(default=None, max_length=5000)

    @model_validator(mode="after")
    def _comment(self) -> FindingResolution:
        if self.status is FindingStatus.DISMISSED and not (self.comment and self.comment.strip()):
            raise ValueError("Відхилення зауваження потребує обґрунтування")
        return self


# --- Read ---------------------------------------------------------------------

class AiRunRead(ReadModel):
    id: uuid.UUID
    task_type: AiTaskType
    status: AiRunStatus
    document_id: uuid.UUID | None
    document_version_id: uuid.UUID | None
    model_id: uuid.UUID
    confidence: Decimal | None
    confidence_threshold: Decimal = CONFIDENCE_THRESHOLD
    requires_human_review: bool
    human_review_status: HumanReviewStatus
    reviewed_by_id: uuid.UUID | None
    reviewed_at: datetime | None
    pii_masked: bool
    queued_at: datetime
    finished_at: datetime | None


class ExtractedFieldRead(ReadModel):
    id: uuid.UUID
    field_key: str
    extracted_value: Any
    validated_value: Any
    confidence: Decimal
    requires_human_review: bool
    human_review_status: HumanReviewStatus
    page_no: int | None


class CitationRead(ReadModel):
    id: uuid.UUID
    chunk_id: uuid.UUID
    rank: int
    quoted_text: str
    quote_verified: bool
    fused_score: float | None


class FindingRead(ReadModel):
    id: uuid.UUID
    ordinal: int
    severity: FindingSeverity
    category: FindingCategory
    title: str
    explanation: str
    recommendation: str | None
    char_start: int | None
    char_end: int | None
    confidence: Decimal
    requires_human_review: bool
    status: FindingStatus
    citations: list[CitationRead] = Field(default_factory=list)


class ComplianceCheckRead(ReadModel):
    id: uuid.UUID
    ai_run_id: uuid.UUID
    document_version_id: uuid.UUID
    verdict: ComplianceVerdict
    summary: str | None
    regulations_as_of: date
    created_at: datetime
    findings: list[FindingRead] = Field(default_factory=list)
