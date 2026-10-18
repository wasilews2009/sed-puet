"""Відповідь пайплайна / API: валідовані метадані разом із confidence score."""

from __future__ import annotations

import uuid
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

from sed_db.base import CONFIDENCE_THRESHOLD
from sed_db.enums import AccessLevel, DocumentDirection

from .models import TextSource
from .requisites import DocumentRequisites


class IngestStatus(StrEnum):
    PROCESSED = "processed"                          # усі реквізити ≥ 0.85
    NEEDS_REVIEW = "needs_review"                    # є реквізити < 0.85 → черга оператора
    MANUAL_ENTRY_REQUIRED = "manual_entry_required"  # текст не розпізнано або LLM недоступна


class StoredFileInfo(BaseModel):
    file_name: str
    mime_type: str
    size_bytes: int
    sha256: str
    storage_bucket: str
    storage_key: str
    storage_version_id: str | None


class OcrPageInfo(BaseModel):
    page_no: int
    source: TextSource
    quality: Decimal = Field(description="1.0 — текстовий шар PDF; для OCR — середня впевненість Tesseract")
    chars: int
    note: str | None = None


class OcrSummary(BaseModel):
    engine: str
    page_count: int
    pages_processed: int
    used_ocr: bool
    quality: Decimal
    pages: list[OcrPageInfo]


class AiRunRefs(BaseModel):
    ocr: uuid.UUID
    extraction: uuid.UUID


class IngestResponse(BaseModel):
    document_id: uuid.UUID
    version_id: uuid.UUID
    status: IngestStatus
    document_type_code: str = Field(description="Код виду документа в довіднику СЕД, куди зараховано картку")
    direction: DocumentDirection
    access_level: AccessLevel
    file: StoredFileInfo
    ocr: OcrSummary
    requisites: DocumentRequisites | None = Field(
        description="Реквізити за ДСТУ 4163:2020; null — модель недоступна, потрібне ручне введення")
    confidence: Decimal = Field(ge=0, le=1, description="Підсумкова впевненість (мінімум по реквізитах)")
    confidence_threshold: Decimal = CONFIDENCE_THRESHOLD
    requires_human_review: bool
    review_reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    duplicate_of: uuid.UUID | None = Field(default=None, description="Документ з ідентичним файлом (SHA-256)")
    ai_runs: AiRunRefs
