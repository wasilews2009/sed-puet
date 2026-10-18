"""Перевірка доступу до версії документа і збереження комплаєнс-перевірки в PostgreSQL.

Одна транзакція: `ai_runs` (task `compliance_check`, `pii_masked = true`) → `compliance_checks` →
`compliance_findings` → `compliance_citations`. Тригер на COMMIT пише в `audit_logs` вердикт,
confidence і масив порушень з цитатами (`violations`). Дані проходять DTO `sed_db.schemas`
(`AiRunCreate`, `AiRunResult`, `ComplianceCheckIn`), тож бізнес-правила (хоча б одна перевірена
цитата на зауваження, вердикт ↔ серйозність) перевіряються ще до БД.

Актор транзакції — `ai_service`: guard-тригери БД не дадуть цій ролі закрити зауваження,
підтвердити власну перевірку чи змінити статус документа.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from datetime import date

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sed_db.enums import AiRunStatus, AiTaskType, HumanReviewStatus, ModelProvider, UserRole
from sed_db.models import (
    AiModel,
    AiRun,
    ComplianceCheck,
    ComplianceCitation,
    ComplianceFinding,
    Document,
    DocumentType,
    DocumentVersion,
    PromptTemplate,
    RegulatoryChunk,
    User,
    UserRoleAssignment,
)
from sed_db.schemas import AiRunCreate, AiRunResult
from sed_db.schemas.ai import CitationIn, ComplianceCheckIn, FindingIn

from ..domain.models import AccessDenied, ConfigurationError, NotFound
from ..ports import CheckRecord, PiiMasker, SavedCheck, VersionForCheck
from .sql_corpus import set_actor

_UNIVERSITY_WIDE = (UserRole.RECTOR, UserRole.VICE_RECTOR)


class SqlComplianceRepository:
    """Реалізація порту `ComplianceRepository`."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], masker: PiiMasker) -> None:
        self.sessionmaker = sessionmaker
        self.masker = masker

    async def load_version(self, keycloak_sub: uuid.UUID, version_id: uuid.UUID) -> VersionForCheck:
        today = date.today()
        async with self.sessionmaker() as s:
            user = (await s.execute(select(User.id, User.is_active, User.primary_org_unit_id)
                                    .where(User.keycloak_sub == keycloak_sub))).one_or_none()
            if user is None or not user.is_active:
                raise AccessDenied("Обліковий запис не знайдено в СЕД або його деактивовано")
            row = (await s.execute(
                select(DocumentVersion.id, DocumentVersion.extracted_text, Document.id.label("doc_id"),
                       Document.title, Document.author_id, Document.org_unit_id, Document.registration_date,
                       Document.deleted_at, DocumentType.code)
                .join(Document, Document.id == DocumentVersion.document_id)
                .join(DocumentType, DocumentType.id == Document.document_type_id)
                .where(DocumentVersion.id == version_id)
            )).one_or_none()
            if row is None or row.deleted_at is not None:
                raise NotFound("Версію документа не знайдено")
            allowed = row.author_id == user.id or row.org_unit_id == user.primary_org_unit_id
            if not allowed:
                allowed = await s.scalar(
                    select(UserRoleAssignment.id).where(
                        UserRoleAssignment.user_id == user.id,
                        UserRoleAssignment.valid_from <= today,
                        or_(UserRoleAssignment.valid_to.is_(None), UserRoleAssignment.valid_to > today),
                        or_(UserRoleAssignment.org_unit_id == row.org_unit_id,
                            and_(UserRoleAssignment.org_unit_id.is_(None),
                                 UserRoleAssignment.role.in_(_UNIVERSITY_WIDE))),
                    ).limit(1)) is not None
            if not allowed:
                raise AccessDenied("Немає доступу до цього документа")
            if not (row.extracted_text or "").strip():
                raise NotFound("У версії документа немає тексту: спершу розпізнайте або завантажте текст")
            return VersionForCheck(
                user_id=user.id, document_id=row.doc_id, version_id=row.id, text=row.extracted_text,
                title=row.title, document_type_code=row.code, document_date=row.registration_date,
            )

    async def save_check(self, r: CheckRecord) -> SavedCheck:
        rep = r.report
        chunk_ids = {c.chunk_id for f in rep.findings for c in f.citations}
        async with self.sessionmaker() as s, s.begin():
            await set_actor(s, r.version.user_id, "ai_service", r.request_id)
            await self._ensure_chunks(s, chunk_ids)
            model_id = await self._model_id(s, r)
            prompt_ids = [await self._prompt_id(s, code, ver, body, r.version.user_id)
                          for code, ver, body in r.prompts]
            AiRunCreate(task_type=AiTaskType.COMPLIANCE_CHECK, model_id=model_id, prompt_template_id=prompt_ids[0],
                        document_version_id=r.version.version_id, pii_masked=True)

            findings = [
                FindingIn(
                    severity=f.severity, category=f.category,
                    # Заголовок потрапляє в audit_logs.violations — зберігаємо його з мітками ПД.
                    title=self.masker.mask(f.title).masked_text[:512],
                    explanation=f.explanation, recommendation=f.recommendation,
                    fragment_text=f.fragment.text, char_start=f.fragment.char_start, char_end=f.fragment.char_end,
                    confidence=f.confidence,
                    citations=[CitationIn(chunk_id=c.chunk_id, quoted_text=c.quoted_text, quote_verified=c.quote_verified,
                                          dense_score=c.dense_score, sparse_score=c.sparse_score,
                                          fused_score=c.fused_score, rerank_score=c.rerank_score)
                               for c in f.citations],
                )
                for f in rep.findings
            ]
            result = AiRunResult(
                status=AiRunStatus.SUCCEEDED, confidence=rep.confidence, input_sha256=r.input_sha256,
                pii_entities_masked=r.pii_entities_masked,
                output={"verdict": rep.verdict.value, "raw": r.raw_output, "warnings": rep.warnings,
                        "review_reasons": [self.masker.mask(x).masked_text for x in rep.review_reasons],
                        "provisions": [p.model_dump(mode="json") for p in rep.provisions]},
                tokens_in=r.tokens_in, tokens_out=r.tokens_out, latency_ms=r.latency_ms,
                compliance_check=ComplianceCheckIn(
                    verdict=rep.verdict, summary=rep.summary, regulations_as_of=rep.regulations_as_of,
                    retrieval_config=rep.retrieval_config, sources_considered=rep.sources_considered,
                    findings=findings,
                ),
            )
            review = (HumanReviewStatus.PENDING if rep.requires_human_review or result.requires_human_review
                      else HumanReviewStatus.NOT_REQUIRED)
            run = AiRun(
                task_type=AiTaskType.COMPLIANCE_CHECK, status=result.status, document_id=r.version.document_id,
                document_version_id=r.version.version_id, model_id=model_id, prompt_template_id=prompt_ids[0],
                initiated_by_id=r.version.user_id, pii_masked=True, pii_entities_masked=result.pii_entities_masked,
                input_sha256=result.input_sha256, output=result.output, confidence=result.confidence,
                human_review_status=review, tokens_in=result.tokens_in, tokens_out=result.tokens_out,
                latency_ms=result.latency_ms, started_at=r.started_at, finished_at=r.finished_at,
                parameters={"temperature": 0, "model": r.llm_name, "prompts": {
                    code: {"version": ver, "id": str(pid)} for (code, ver, _), pid in zip(r.prompts, prompt_ids, strict=True)},
                    "retrieval": rep.retrieval_config},
            )
            s.add(run)
            await s.flush()
            cc = result.compliance_check
            assert cc is not None
            check = ComplianceCheck(
                ai_run_id=run.id, document_version_id=r.version.version_id, verdict=cc.verdict, summary=cc.summary,
                regulations_as_of=cc.regulations_as_of, retrieval_config=cc.retrieval_config,
                sources_considered=cc.sources_considered,
            )
            s.add(check)
            await s.flush()
            statuses = {c.chunk_id: c.source_status for f in rep.findings for c in f.citations}
            for n, f in enumerate(cc.findings, start=1):
                row = ComplianceFinding(
                    check_id=check.id, ordinal=n, severity=f.severity, category=f.category, title=f.title,
                    explanation=f.explanation, recommendation=f.recommendation, fragment_text=f.fragment_text,
                    char_start=f.char_start, char_end=f.char_end, confidence=f.confidence,
                )
                s.add(row)
                await s.flush()
                for rank, c in enumerate(f.citations, start=1):
                    s.add(ComplianceCitation(
                        finding_id=row.id, chunk_id=c.chunk_id, rank=rank, quoted_text=c.quoted_text,
                        quote_verified=c.quote_verified, source_status_snapshot=statuses[c.chunk_id],
                        dense_score=c.dense_score, sparse_score=c.sparse_score, fused_score=c.fused_score,
                        rerank_score=c.rerank_score,
                    ))
            await s.flush()
            return SavedCheck(ai_run_id=run.id, check_id=check.id)

    async def _ensure_chunks(self, s: AsyncSession, ids: Sequence[uuid.UUID] | set[uuid.UUID]) -> None:
        if not ids:
            return
        found = set((await s.scalars(select(RegulatoryChunk.id).where(RegulatoryChunk.id.in_(list(ids))))).all())
        missing = set(ids) - found
        if missing:
            raise ConfigurationError(
                f"Індекс Qdrant містить {len(missing)} фрагмент(ів), яких немає в regulatory_chunks: "
                "переіндексуйте корпус (python -m sed_rag.ingestion.cli reconcile)")

    async def _model_id(self, s: AsyncSession, r: CheckRecord) -> uuid.UUID:
        await s.execute(insert(AiModel).values(name=r.llm_name, version=r.llm_version,
                                               provider=ModelProvider(r.llm_provider),
                                               task_type=AiTaskType.COMPLIANCE_CHECK)
                        .on_conflict_do_nothing(index_elements=["name", "version"]))
        return await s.scalar(select(AiModel.id).where(AiModel.name == r.llm_name,  # type: ignore[return-value]
                                                       AiModel.version == r.llm_version))

    async def _prompt_id(self, s: AsyncSession, code: str, version: int, body: str, user_id: uuid.UUID) -> uuid.UUID:
        digest = hashlib.sha256(body.encode()).hexdigest()
        await s.execute(insert(PromptTemplate).values(
            code=code, version=version, task_type=AiTaskType.COMPLIANCE_CHECK, body=body, body_sha256=digest,
            created_by_id=user_id,
        ).on_conflict_do_nothing(index_elements=["code", "version"]))
        row = (await s.execute(select(PromptTemplate.id, PromptTemplate.body_sha256)
                               .where(PromptTemplate.code == code, PromptTemplate.version == version))).one()
        if row.body_sha256 != digest:
            raise ConfigurationError(f"Текст промпту {code} v{version} змінено без підвищення версії")
        return row.id

