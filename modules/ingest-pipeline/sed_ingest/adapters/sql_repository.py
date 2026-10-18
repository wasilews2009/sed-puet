"""Збереження результатів інжесту в PostgreSQL через моделі `sed_db` (async SQLAlchemy 2.0).

Одна транзакція на документ: картка (draft) → версія 1 з текстовим шаром → запуск OCR →
запуск NER з реквізитами. Тригер `audit_logs` на COMMIT пише підсумок аналізу ШІ
(confidence, вилучені метадані; поля з `"x-pii": true` у схемі виду — замасковано).

Вхідні дані проганяються через DTO `sed_db.schemas.AiRunCreate`/`AiRunResult`, тож ті самі
бізнес-правила (PII для LLM, confidence для успішного запуску, унікальні ключі реквізитів)
перевіряються до запиту в БД, а CHECK-обмеження БД лишаються другим рубежем.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sed_db.base import CONFIDENCE_THRESHOLD
from sed_db.enums import (
    AiRunStatus,
    AiTaskType,
    DocumentStatus,
    HumanReviewStatus,
    ModelProvider,
    UserRole,
    VersionSource,
)
from sed_db.models import (
    AiExtractedField,
    AiModel,
    AiRun,
    Document,
    DocumentType,
    DocumentVersion,
    OrgUnit,
    PromptTemplate,
    User,
    UserRoleAssignment,
)
from sed_db.schemas import AiRunCreate, AiRunResult, ExtractedFieldIn

from ..domain.models import AccessDenied, ConfigurationError, IngestRecord, SavedIngest, Uploader
from ..domain.requisites import DocumentKind, quantize_confidence

_UNIVERSITY_WIDE = (UserRole.RECTOR, UserRole.VICE_RECTOR)


def _review(confidence: Decimal | None) -> HumanReviewStatus:
    return (HumanReviewStatus.PENDING if confidence is not None and confidence < CONFIDENCE_THRESHOLD
            else HumanReviewStatus.NOT_REQUIRED)


class SqlIngestRepository:
    """Реалізація порту `IngestRepository`."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        type_codes: dict[DocumentKind, str],
        fallback_type_code: str,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.type_codes = type_codes
        self.fallback_type_code = fallback_type_code

    # --- доступ --------------------------------------------------------------------

    async def resolve_uploader(self, keycloak_sub: uuid.UUID, org_unit_id: uuid.UUID) -> Uploader:
        today = date.today()
        async with self.sessionmaker() as s:
            user = (await s.execute(
                select(User.id, User.is_active, User.primary_org_unit_id).where(User.keycloak_sub == keycloak_sub)
            )).one_or_none()
            if user is None or not user.is_active:
                raise AccessDenied("Обліковий запис не знайдено в СЕД або його деактивовано")
            if await s.scalar(select(OrgUnit.id).where(OrgUnit.id == org_unit_id)) is None:
                raise AccessDenied("Підрозділ не знайдено")
            if user.primary_org_unit_id == org_unit_id:
                return Uploader(user_id=user.id, org_unit_id=org_unit_id)
            has_role = await s.scalar(
                select(UserRoleAssignment.id).where(
                    UserRoleAssignment.user_id == user.id,
                    UserRoleAssignment.valid_from <= today,
                    or_(UserRoleAssignment.valid_to.is_(None), UserRoleAssignment.valid_to > today),
                    or_(UserRoleAssignment.org_unit_id == org_unit_id,
                        and_(UserRoleAssignment.org_unit_id.is_(None),
                             UserRoleAssignment.role.in_(_UNIVERSITY_WIDE))),
                ).limit(1)
            )
            if has_role is None:
                raise AccessDenied("Немає права реєструвати документи цього підрозділу")
            return Uploader(user_id=user.id, org_unit_id=org_unit_id)

    async def find_duplicate(self, content_sha256: str) -> uuid.UUID | None:
        async with self.sessionmaker() as s:
            return await s.scalar(
                select(DocumentVersion.document_id)
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(DocumentVersion.content_sha256 == content_sha256, Document.deleted_at.is_(None))
                .limit(1)
            )

    # --- запис ---------------------------------------------------------------------

    async def save(self, r: IngestRecord) -> SavedIngest:
        async with self.sessionmaker() as s, s.begin():
            await s.execute(
                text("SELECT set_config('sed.actor_user_id', :u, true),"
                     "       set_config('sed.actor_type', 'user', true),"
                     "       set_config('sed.request_id', :r, true)"),
                {"u": str(r.uploader.user_id), "r": r.command.request_id},
            )
            dtype = await self._document_type(s, r.kind)
            direction = r.direction or dtype.default_direction
            access = r.access_level or dtype.default_access_level

            doc = Document(
                id=r.document_id, document_type_id=dtype.id, direction=direction,
                status=DocumentStatus.DRAFT, access_level=access, title=r.title,
                author_id=r.uploader.user_id, org_unit_id=r.uploader.org_unit_id,
                correspondent_name=r.command.correspondent_name,
            )
            s.add(doc)
            await s.flush()
            ver = DocumentVersion(
                id=r.version_id, document_id=doc.id, version_number=1,
                source=VersionSource.OCR_SCAN if r.ocr.used_ocr else VersionSource.UPLOAD,
                storage_bucket=r.stored.bucket, storage_key=r.stored.key,
                storage_version_id=r.stored.version_id, file_name=r.command.file.file_name,
                mime_type=r.command.file.mime_type, size_bytes=r.command.file.size,
                content_sha256=r.command.file.sha256, page_count=r.ocr.page_count,
                extracted_text=r.ocr.text or None, created_by_id=r.uploader.user_id,
            )
            s.add(ver)
            await s.flush()
            doc.current_version_id = ver.id
            await s.flush()

            ocr_run = await self._ocr_run(s, r)
            ner_run = await self._extraction_run(s, r)
            await s.flush()
            return SavedIngest(
                document_id=doc.id, version_id=ver.id, ocr_run_id=ocr_run.id,
                extraction_run_id=ner_run.id, document_type_code=dtype.code,
                access_level=access, direction=direction,
            )

    async def _document_type(self, s: AsyncSession, kind: DocumentKind | None) -> DocumentType:
        codes = [self.type_codes[kind]] if kind is not None and kind in self.type_codes else []
        codes.append(self.fallback_type_code)
        for code in codes:
            dtype = await s.scalar(select(DocumentType).where(DocumentType.code == code,
                                                              DocumentType.is_active.is_(True)))
            if dtype is not None:
                return dtype
        raise ConfigurationError(f"У довіднику видів документів немає активного виду «{self.fallback_type_code}»")

    async def _model_id(self, s: AsyncSession, name: str, version: str,
                        provider: ModelProvider, task: AiTaskType) -> uuid.UUID:
        await s.execute(insert(AiModel).values(name=name, version=version, provider=provider, task_type=task)
                        .on_conflict_do_nothing(index_elements=["name", "version"]))
        return (await s.scalar(select(AiModel.id).where(AiModel.name == name, AiModel.version == version)))  # type: ignore[return-value]

    async def _prompt_id(self, s: AsyncSession, r: IngestRecord) -> uuid.UUID:
        digest = hashlib.sha256(r.prompt_body.encode()).hexdigest()
        await s.execute(insert(PromptTemplate).values(
            code=r.prompt_code, version=r.prompt_version, task_type=AiTaskType.NER_EXTRACTION,
            body=r.prompt_body, body_sha256=digest, created_by_id=r.uploader.user_id,
        ).on_conflict_do_nothing(index_elements=["code", "version"]))
        row = (await s.execute(select(PromptTemplate.id, PromptTemplate.body_sha256).where(
            PromptTemplate.code == r.prompt_code, PromptTemplate.version == r.prompt_version))).one()
        if row.body_sha256 != digest:
            raise ConfigurationError(
                f"Текст промпту {r.prompt_code} v{r.prompt_version} змінено без підвищення версії")
        return row.id

    async def _ocr_run(self, s: AsyncSession, r: IngestRecord) -> AiRun:
        model_id = await self._model_id(s, "tesseract", r.ocr.engine_version,
                                        ModelProvider.LOCAL_OTHER, AiTaskType.OCR)
        confidence = quantize_confidence(r.ocr.quality)
        AiRunCreate(task_type=AiTaskType.OCR, model_id=model_id, document_version_id=r.version_id,
                    pii_masked=False)
        result = AiRunResult(
            status=AiRunStatus.SUCCEEDED, confidence=confidence, latency_ms=r.ocr.latency_ms,
            output={
                "engine": r.ocr.engine, "page_count": r.ocr.page_count, "truncated": r.ocr.truncated,
                "pages": [{"page_no": p.page_no, "source": p.source.value, "quality": round(p.quality, 4),
                           "chars": len(p.text), "note": p.note} for p in r.ocr.pages],
            },
        )
        run = AiRun(
            task_type=AiTaskType.OCR, status=result.status, document_id=r.document_id,
            document_version_id=r.version_id, model_id=model_id, initiated_by_id=r.uploader.user_id,
            pii_masked=False, parameters={"engine": r.ocr.engine}, output=result.output,
            confidence=result.confidence, human_review_status=_review(result.confidence),
            latency_ms=result.latency_ms, started_at=r.ocr_started_at, finished_at=r.ocr_finished_at,
        )
        s.add(run)
        return run

    async def _extraction_run(self, s: AsyncSession, r: IngestRecord) -> AiRun:
        x = r.extraction
        model_id = await self._model_id(s, r.llm.name, r.llm.version, ModelProvider(r.llm.provider),
                                        AiTaskType.NER_EXTRACTION)
        prompt_id = await self._prompt_id(s, r)
        AiRunCreate(task_type=AiTaskType.NER_EXTRACTION, model_id=model_id, prompt_template_id=prompt_id,
                    document_version_id=r.version_id, pii_masked=True)

        fields: list[ExtractedFieldIn] = []
        if x.requisites is not None:
            for key, f in x.requisites.fields().items():
                fields.append(ExtractedFieldIn(field_key=key, extracted_value=_jsonable(f.value),
                                               confidence=f.confidence, page_no=f.page_no))
            output: dict[str, Any] | None = {
                "label": x.requisites.document_type.value,   # клас документа → audit_logs.ai_verdict
                "raw": x.masked_output,                      # відповідь моделі з мітками ПД
                "issues": {k: f.issues for k, f in x.requisites.fields().items() if f.issues},
                "warnings": x.warnings,
            }
            result = AiRunResult(
                status=AiRunStatus.SUCCEEDED, confidence=x.requisites.confidence,
                input_sha256=x.input_sha256, pii_entities_masked=x.pii_entities_masked, output=output,
                tokens_in=x.completion.tokens_in if x.completion else None,
                tokens_out=x.completion.tokens_out if x.completion else None,
                latency_ms=x.completion.latency_ms if x.completion else None,
                extracted_fields=fields,
            )
            review = (HumanReviewStatus.PENDING if result.requires_human_review
                      else HumanReviewStatus.NOT_REQUIRED)
        else:
            result = AiRunResult(status=AiRunStatus.FAILED, error_message=x.error or "Невідома помилка",
                                 input_sha256=x.input_sha256, pii_entities_masked=x.pii_entities_masked)
            review = HumanReviewStatus.PENDING   # реквізити вводить оператор — документ у його черзі

        run = AiRun(
            task_type=AiTaskType.NER_EXTRACTION, status=result.status, document_id=r.document_id,
            document_version_id=r.version_id, model_id=model_id, prompt_template_id=prompt_id,
            initiated_by_id=r.uploader.user_id, pii_masked=True, pii_entities_masked=result.pii_entities_masked,
            input_sha256=result.input_sha256,
            parameters={"temperature": 0, "schema": "dstu4163_requisites", "model": r.llm.name},
            output=result.output, confidence=result.confidence, human_review_status=review,
            tokens_in=result.tokens_in, tokens_out=result.tokens_out, latency_ms=result.latency_ms,
            error_message=result.error_message, started_at=x.started_at, finished_at=x.finished_at,
        )
        s.add(run)
        await s.flush()
        for f in result.extracted_fields:
            s.add(AiExtractedField(
                ai_run_id=run.id, document_version_id=r.version_id, field_key=f.field_key,
                extracted_value=f.extracted_value, confidence=f.confidence, page_no=f.page_no,
                human_review_status=_review(f.confidence),
            ))
        return run


def _jsonable(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, date):
        return value.isoformat()
    return value if isinstance(value, (str, int, float, bool, dict)) else str(value)
