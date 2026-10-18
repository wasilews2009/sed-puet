"""Оркестратор інжесту: MinIO → OCR → PII → LLM → скоринг → БД.

Не залежить від FastAPI: той самий `IngestPipeline.run()` викликається з ендпоінта
(синхронний режим) або з Arq/Celery-задачі (фонова обробка великих пакетів сканів).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

import anyio

from sed_db.enums import AccessLevel

from ..domain.models import IngestCommand, IngestRecord, LlmIdentity, SavedIngest
from ..domain.requisites import KIND_LABELS_UK, DocumentRequisites, quantize_confidence
from ..domain.result import (
    AiRunRefs,
    IngestResponse,
    IngestStatus,
    OcrPageInfo,
    OcrSummary,
    StoredFileInfo,
)
from ..ports import IngestRepository, ObjectStorage, OcrEngine
from .extraction import RequisitesExtractor
from .prompts import PROMPT_CODE, PROMPT_VERSION, SYSTEM_PROMPT
from .scoring import FIELD_LABELS_UK

log = logging.getLogger(__name__)

# Категорії ПД, наявність яких робить документ документом з персональними даними.
SENSITIVE_PII = frozenset({"РНОКПП", "ПАСПОРТ", "РАХУНОК"})


class IngestPipeline:
    def __init__(
        self,
        storage: ObjectStorage,
        ocr: OcrEngine,
        extractor: RequisitesExtractor,
        repository: IngestRepository,
        llm_identity: LlmIdentity,
        *,
        ocr_concurrency: int = 2,
    ) -> None:
        self.storage = storage
        self.ocr = ocr
        self.extractor = extractor
        self.repository = repository
        self.llm_identity = llm_identity
        # OCR — CPU-bound: обмежуємо паралельність, щоб скани не «з'їли» всі ядра API-вузла.
        self._ocr_limiter = anyio.CapacityLimiter(ocr_concurrency)

    async def run(self, cmd: IngestCommand) -> IngestResponse:
        uploader = await self.repository.resolve_uploader(cmd.uploader_sub, cmd.org_unit_id)
        duplicate_of = await self.repository.find_duplicate(cmd.file.sha256)

        document_id, version_id = uuid.uuid4(), uuid.uuid4()
        key = f"documents/{document_id}/v1/{version_id}.{cmd.file.file_name.rsplit('.', 1)[-1]}"

        # 1. Оригінал у MinIO
        stored = await self.storage.put(key, cmd.file, {
            "document-id": str(document_id), "version-id": str(version_id),
            "uploaded-by": str(uploader.user_id), "request-id": cmd.request_id,
        })
        try:
            # 2. OCR (текстовий шар → Tesseract)
            ocr_started = datetime.now(timezone.utc)
            ocr = await anyio.to_thread.run_sync(self.ocr.extract, cmd.file, limiter=self._ocr_limiter)
            ocr_finished = datetime.now(timezone.utc)

            # 3. LLM structured output + детермінований скоринг
            extraction = await self.extractor.extract(ocr)
            req = extraction.requisites

            # 4. Збереження (одна транзакція)
            kind = req.document_type.value if req and not req.document_type.requires_human_review else None
            access = cmd.access_level
            sensitive = bool(SENSITIVE_PII & set(extraction.pii_counts))
            warnings = list(extraction.warnings)
            if sensitive and access in (None, AccessLevel.PUBLIC, AccessLevel.INTERNAL):
                access = AccessLevel.PERSONAL_DATA
                warnings.append("Знайдено РНОКПП / паспортні / банківські дані: гриф «містить персональні дані»")
            record = IngestRecord(
                document_id=document_id, version_id=version_id, command=cmd, uploader=uploader,
                stored=stored, ocr=ocr, ocr_started_at=ocr_started, ocr_finished_at=ocr_finished,
                extraction=extraction, kind=kind, title=_title(req, cmd.file.file_name),
                direction=cmd.direction, access_level=access, llm=self.llm_identity,
                prompt_code=PROMPT_CODE, prompt_version=PROMPT_VERSION, prompt_body=SYSTEM_PROMPT,
            )
            saved = await self.repository.save(record)
        except BaseException:
            # Компенсація: у сховищі не лишається файлів без картки в БД.
            with anyio.CancelScope(shield=True):
                await self.storage.delete(stored)
            raise

        if duplicate_of is not None:
            warnings.append("Такий самий файл уже зареєстровано в СЕД — перевірте, чи це не дублікат")
        return _response(record, saved, warnings, duplicate_of)


def _title(req: DocumentRequisites | None, file_name: str) -> str:
    if req is not None and req.title.value:
        return req.title.value[:1024]
    if req is not None and req.document_type.value is not None:
        label = KIND_LABELS_UK[req.document_type.value]
        if req.document_date.value:
            return f"{label} від {req.document_date.value.strftime('%d.%m.%Y')}"
        return label
    return file_name.rsplit(".", 1)[0][:1024]


def _response(r: IngestRecord, saved: SavedIngest, warnings: list[str], duplicate_of: uuid.UUID | None) -> IngestResponse:
    req = r.extraction.requisites
    reasons: list[str] = []
    if req is None:
        status = IngestStatus.MANUAL_ENTRY_REQUIRED
        confidence = quantize_confidence(0)
        reasons.append(r.extraction.error or "Реквізити не вилучено")
    else:
        confidence = req.confidence
        for key, f in req.fields().items():
            if f.requires_human_review:
                reasons.append(f"{FIELD_LABELS_UK[key]}: впевненість {f.confidence} < 0.85"
                               + (f" ({'; '.join(f.issues)})" if f.issues else ""))
        status = IngestStatus.NEEDS_REVIEW if req.requires_human_review else IngestStatus.PROCESSED
    ocr = r.ocr
    return IngestResponse(
        document_id=saved.document_id, version_id=saved.version_id, status=status,
        document_type_code=saved.document_type_code, direction=saved.direction,
        access_level=saved.access_level,
        file=StoredFileInfo(
            file_name=r.command.file.file_name, mime_type=r.command.file.mime_type,
            size_bytes=r.command.file.size, sha256=r.command.file.sha256,
            storage_bucket=r.stored.bucket, storage_key=r.stored.key, storage_version_id=r.stored.version_id,
        ),
        ocr=OcrSummary(
            engine=ocr.engine, page_count=ocr.page_count, pages_processed=len(ocr.pages),
            used_ocr=ocr.used_ocr, quality=quantize_confidence(ocr.quality),
            pages=[OcrPageInfo(page_no=p.page_no, source=p.source, quality=quantize_confidence(p.quality),
                               chars=len(p.text), note=p.note) for p in ocr.pages],
        ),
        requisites=req,
        confidence=confidence,
        requires_human_review=status is not IngestStatus.PROCESSED,
        review_reasons=reasons,
        warnings=warnings,
        duplicate_of=duplicate_of,
        ai_runs=AiRunRefs(ocr=saved.ocr_run_id, extraction=saved.extraction_run_id),
    )
