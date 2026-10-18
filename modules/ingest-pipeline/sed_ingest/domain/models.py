"""Внутрішні об'єкти пайплайна та помилки домену."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from sed_db.enums import AccessLevel, DocumentDirection

from .requisites import DocumentKind, DocumentRequisites


# --- Помилки (API перетворює на HTTP-коди з повідомленням українською) -------------

class IngestError(Exception):
    """Базова помилка інжесту; `message` безпечно показувати користувачу."""

    http_status = 500
    code = "ingest_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class UnsupportedFile(IngestError):
    http_status = 415
    code = "unsupported_file"


class FileRejected(IngestError):
    http_status = 422
    code = "file_rejected"


class AccessDenied(IngestError):
    http_status = 403
    code = "access_denied"


class ConfigurationError(IngestError):
    http_status = 500
    code = "configuration_error"


class StorageUnavailable(IngestError):
    http_status = 503
    code = "storage_unavailable"


class LlmError(IngestError):
    """LLM недоступна або повернула невалідну відповідь (після повторної спроби)."""

    http_status = 502
    code = "llm_error"


# --- Файл -------------------------------------------------------------------------

class FileKind(StrEnum):
    PDF = "pdf"
    PNG = "png"
    JPEG = "jpeg"
    TIFF = "tiff"
    WEBP = "webp"


MIME_BY_KIND: dict[FileKind, str] = {
    FileKind.PDF: "application/pdf",
    FileKind.PNG: "image/png",
    FileKind.JPEG: "image/jpeg",
    FileKind.TIFF: "image/tiff",
    FileKind.WEBP: "image/webp",
}


@dataclass(frozen=True, slots=True)
class IncomingFile:
    data: bytes
    file_name: str
    kind: FileKind
    sha256: str

    @property
    def mime_type(self) -> str:
        return MIME_BY_KIND[self.kind]

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass(frozen=True, slots=True)
class StoredObject:
    bucket: str
    key: str
    version_id: str | None
    etag: str | None


# --- OCR --------------------------------------------------------------------------

class TextSource(StrEnum):
    TEXT_LAYER = "text_layer"   # текстовий шар PDF
    OCR = "ocr"                 # Tesseract
    EMPTY = "empty"             # нічого не розпізнано


@dataclass(frozen=True, slots=True)
class PageText:
    page_no: int
    text: str
    source: TextSource
    quality: float              # 1.0 — текстовий шар; середня впевненість Tesseract/100 для OCR
    note: str | None = None     # напр. «текстовий шар зіпсований — розпізнано OCR»
    words: tuple[tuple[str, float], ...] = ()   # слова OCR з впевненістю 0…1 (порожньо для текстового шару)

    def value_quality(self, value: str) -> float:
        """Мінімальна впевненість Tesseract серед слів, з яких складається значення.
        «№ 245-од», розпізнане як «245-00» з conf 0.58, не має отримати якість усієї сторінки."""
        if self.source is not TextSource.OCR or not self.words:
            return self.quality
        confs: list[float] = []
        for token in value.split():
            t = token.strip(".,;:«»\"()").casefold()
            if len(t) < 2 or "[" in t:
                continue
            hits = [c for w, c in self.words if t in w.casefold() or (len(w) >= 3 and w.casefold() in t)]
            if hits:
                confs.append(max(hits))
        return min(confs) if confs else self.quality


@dataclass(frozen=True, slots=True)
class OcrResult:
    pages: tuple[PageText, ...]
    engine: str                 # «tesseract 5.3.4 ukr+eng» / «pdf-text-layer»
    engine_version: str
    page_count: int             # усього сторінок у файлі (можуть бути оброблені не всі)
    truncated: bool = False     # сторінок більше за ліміт
    latency_ms: int = 0

    @property
    def text(self) -> str:
        return "\n\f\n".join(p.text for p in self.pages)

    @property
    def used_ocr(self) -> bool:
        return any(p.source is TextSource.OCR for p in self.pages)

    @property
    def quality(self) -> float:
        """Середня якість, зважена довжиною тексту сторінки."""
        total = sum(len(p.text) for p in self.pages)
        if total == 0:
            return 0.0
        return sum(p.quality * len(p.text) for p in self.pages) / total

    def page_of_offset(self, offset: int) -> int | None:
        pos = 0
        for p in self.pages:
            end = pos + len(p.text)
            if pos <= offset <= end:
                return p.page_no
            pos = end + 3  # роздільник "\n\f\n"
        return None

    def quality_of_page(self, page_no: int | None, value: str | None = None) -> float:
        if page_no is None:
            return self.quality
        for p in self.pages:
            if p.page_no == page_no:
                return p.value_quality(value) if value else p.quality
        return self.quality


# --- PII --------------------------------------------------------------------------

@dataclass(slots=True)
class MaskingResult:
    masked_text: str
    mapping: dict[str, str]                      # мітка → оригінал (лише в пам'яті процесу)
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def entities_masked(self) -> int:
        return sum(self.counts.values())


# --- LLM --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class LlmCompletion:
    content: dict[str, Any]       # розібраний JSON (ще не валідований Pydantic)
    model: str
    tokens_in: int | None
    tokens_out: int | None
    latency_ms: int
    attempts: int


@dataclass(frozen=True, slots=True)
class LlmIdentity:
    name: str
    version: str
    provider: str                 # значення sed_db.enums.ModelProvider


# --- Запит і результат інжесту ----------------------------------------------------

@dataclass(frozen=True, slots=True)
class IngestCommand:
    file: IncomingFile
    uploader_sub: uuid.UUID                     # Keycloak `sub`
    org_unit_id: uuid.UUID
    direction: DocumentDirection | None
    access_level: AccessLevel | None
    correspondent_name: str | None
    request_id: str


@dataclass(frozen=True, slots=True)
class Uploader:
    user_id: uuid.UUID
    org_unit_id: uuid.UUID


@dataclass(slots=True)
class ExtractionOutcome:
    requisites: DocumentRequisites | None       # None — LLM недоступна, реквізити вводить людина
    masked_output: dict[str, Any] | None        # що повернула модель (з мітками ПД) — для ai_runs.output
    input_sha256: str | None
    pii_entities_masked: int
    completion: LlmCompletion | None
    error: str | None
    started_at: datetime
    finished_at: datetime
    warnings: list[str] = field(default_factory=list)
    pii_counts: dict[str, int] = field(default_factory=dict)   # категорія → кількість (без значень)


@dataclass(slots=True)
class IngestRecord:
    """Усе, що зберігається однією транзакцією після обробки файлу."""

    document_id: uuid.UUID
    version_id: uuid.UUID
    command: IngestCommand
    uploader: Uploader
    stored: StoredObject
    ocr: OcrResult
    ocr_started_at: datetime
    ocr_finished_at: datetime
    extraction: ExtractionOutcome
    kind: DocumentKind | None
    title: str
    direction: DocumentDirection | None
    access_level: AccessLevel | None
    llm: LlmIdentity
    prompt_code: str
    prompt_version: int
    prompt_body: str


@dataclass(frozen=True, slots=True)
class SavedIngest:
    document_id: uuid.UUID
    version_id: uuid.UUID
    ocr_run_id: uuid.UUID
    extraction_run_id: uuid.UUID
    document_type_code: str
    access_level: AccessLevel
    direction: DocumentDirection
