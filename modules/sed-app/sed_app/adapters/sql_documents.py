"""Читання документів для веб-інтерфейсу: список з фільтром доступу, картка, черги.

Читаються таблиці `sed_db` напряму (read model). Пишуть у них модулі інжесту, комплаєнсу і
маршрутів, а цей адаптер — лише рішення людини (ручна валідація, див. `sql_review.py`).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from sed_db.enums import (
    AccessLevel,
    AiTaskType,
    DocumentStatus,
    FindingSeverity,
    FindingStatus,
    HumanReviewStatus,
    RouteStatus,
    StepStatus,
)
from sed_db.models import (
    AiExtractedField,
    AiModel,
    AiRun,
    ApprovalRoute,
    ApprovalStep,
    ComplianceCheck,
    ComplianceCitation,
    ComplianceFinding,
    Document,
    DocumentStatusHistory,
    DocumentType,
    DocumentVersion,
    OrgUnit,
    RegulatoryChunk,
    RegulatorySource,
    RouteTemplate,
    Signature,
    User,
)

from ..api.schemas import (
    AiRunOut,
    CardPermissions,
    CitationOut,
    ComplianceOut,
    DocumentCard,
    DocumentListItem,
    DocumentPage,
    DocumentSort,
    DocumentTypeOut,
    ExtractedFieldOut,
    ExtractionOut,
    FindingOut,
    InboxItem,
    OrgUnitOut,
    PersonRef,
    ReviewQueueItem,
    RouteOut,
    SignatureOut,
    StatusChangeOut,
    StepOut,
    VersionOut,
)
from ..domain.access import (
    MEMBER_VISIBLE_LEVELS,
    AccessScope,
    DocumentFacts,
    can_review,
    can_start_route,
)
from ..domain.errors import NotFound
from ..domain.labels import FIELD_LABELS_UK
from .db import audit_event, set_actor
from .sql_users import display_name

SERIOUS = (FindingSeverity.MAJOR, FindingSeverity.CRITICAL)
_ORDER = {
    DocumentSort.UPDATED_DESC: Document.updated_at.desc(),
    DocumentSort.CREATED_DESC: Document.created_at.desc(),
    DocumentSort.CREATED_ASC: Document.created_at.asc(),
    DocumentSort.TITLE: Document.title.asc(),
}


# --- Політика доступу в SQL (дзеркало sed_app.domain.access.can_view / can_review) ---------

def visible_clause(scope: AccessScope) -> ColumnElement[bool]:
    if scope.all_documents:
        return Document.deleted_at.is_(None)
    participant = exists(
        select(ApprovalStep.id).join(ApprovalRoute, ApprovalRoute.id == ApprovalStep.route_id)
        .where(ApprovalRoute.document_id == Document.id,
               or_(ApprovalStep.assignee_id == scope.user_id, ApprovalStep.original_assignee_id == scope.user_id))
        .correlate(Document)
    )
    conds: list[ColumnElement[bool]] = [
        Document.author_id == scope.user_id, Document.access_level == AccessLevel.PUBLIC, participant]
    if scope.managed_units:
        conds.append(Document.org_unit_id.in_(scope.managed_units))
    if scope.member_units:
        conds.append(and_(Document.org_unit_id.in_(scope.member_units),
                          Document.access_level.in_(MEMBER_VISIBLE_LEVELS)))
    return and_(Document.deleted_at.is_(None), or_(*conds))


def review_clause(scope: AccessScope) -> ColumnElement[bool]:
    """Документи, реквізити яких людина може валідувати (без статусної умови — її додає черга)."""
    if scope.all_documents:
        return Document.deleted_at.is_(None)
    conds: list[ColumnElement[bool]] = [Document.author_id == scope.user_id]
    if scope.managed_units:
        conds.append(Document.org_unit_id.in_(scope.managed_units))
    if scope.member_units:
        conds.append(and_(Document.org_unit_id.in_(scope.member_units),
                          Document.access_level.in_(MEMBER_VISIBLE_LEVELS)))
    return and_(Document.deleted_at.is_(None), or_(*conds))


def _person(user_id: uuid.UUID | None, names: dict[uuid.UUID, str]) -> PersonRef | None:
    return None if user_id is None else PersonRef(id=user_id, name=names.get(user_id, "—"))


async def _names(s: AsyncSession, ids: set[uuid.UUID | None]) -> dict[uuid.UUID, str]:
    ids_ = {i for i in ids if i is not None}
    if not ids_:
        return {}
    rows = (await s.execute(select(User.id, User.first_name, User.last_name).where(User.id.in_(ids_)))).all()
    return {r.id: display_name(r.first_name, r.last_name) for r in rows}


def _issues(output: dict[str, Any] | None, key: str) -> list[str]:
    raw = ((output or {}).get("issues") or {}).get(key) or []
    return [str(x) for x in raw] if isinstance(raw, list) else [str(raw)]


def _run_out(run: AiRun, model: AiModel | None, warnings: list[str] | None = None) -> AiRunOut:
    return AiRunOut(
        id=run.id, status=run.status, model=f"{model.name}:{model.version}" if model else "—",
        confidence=run.confidence, requires_human_review=run.requires_human_review,
        review_status=run.human_review_status, error_message=run.error_message,
        finished_at=run.finished_at, warnings=warnings or [],
    )


class SqlDocumentReader:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self.sessionmaker = sessionmaker

    # --- факти для перевірки доступу ------------------------------------------------------

    async def facts(self, document_id: uuid.UUID, s: AsyncSession | None = None) -> DocumentFacts:
        if s is None:
            async with self.sessionmaker() as s2:
                return await self.facts(document_id, s2)
        row = (await s.execute(
            select(Document.id, Document.author_id, Document.org_unit_id, Document.access_level, Document.status)
            .where(Document.id == document_id, Document.deleted_at.is_(None))
        )).one_or_none()
        if row is None:
            raise NotFound("Документ не знайдено")
        participants = (await s.execute(
            select(ApprovalStep.assignee_id, ApprovalStep.original_assignee_id)
            .join(ApprovalRoute, ApprovalRoute.id == ApprovalStep.route_id)
            .where(ApprovalRoute.document_id == document_id)
        )).all()
        ids = {p.assignee_id for p in participants} | {p.original_assignee_id for p in participants
                                                        if p.original_assignee_id}
        return DocumentFacts(id=row.id, author_id=row.author_id, org_unit_id=row.org_unit_id,
                             access_level=row.access_level, status=row.status,
                             route_participant_ids=frozenset(ids))

    # --- список -------------------------------------------------------------------------

    async def list(self, scope: AccessScope, *, q: str | None = None, status: DocumentStatus | None = None,
                   type_code: str | None = None, org_unit_id: uuid.UUID | None = None,
                   needs_review: bool | None = None, mine: bool = False,
                   sort: DocumentSort = DocumentSort.UPDATED_DESC, limit: int = 25, offset: int = 0) -> DocumentPage:
        cur = Document.current_version_id
        ner_conf = (select(AiRun.confidence)
                    .where(AiRun.document_version_id == cur, AiRun.task_type == AiTaskType.NER_EXTRACTION)
                    .order_by(AiRun.queued_at.desc()).limit(1).correlate(Document).scalar_subquery())
        pending = or_(
            exists(select(AiRun.id).where(AiRun.document_version_id == cur,
                                          AiRun.human_review_status == HumanReviewStatus.PENDING).correlate(Document)),
            exists(select(AiExtractedField.id).where(
                AiExtractedField.document_version_id == cur,
                AiExtractedField.human_review_status == HumanReviewStatus.PENDING).correlate(Document)),
        )
        latest_check = (select(ComplianceCheck.id).where(ComplianceCheck.document_version_id == cur)
                        .order_by(ComplianceCheck.created_at.desc()).limit(1).correlate(Document).scalar_subquery())
        verdict = (select(ComplianceCheck.verdict).where(ComplianceCheck.document_version_id == cur)
                   .order_by(ComplianceCheck.created_at.desc()).limit(1).correlate(Document).scalar_subquery())
        open_findings = (select(func.count(ComplianceFinding.id))
                         .where(ComplianceFinding.check_id == latest_check,
                                ComplianceFinding.status == FindingStatus.OPEN,
                                ComplianceFinding.severity.in_(SERIOUS))
                         .correlate(Document).scalar_subquery())

        where: list[ColumnElement[bool]] = [visible_clause(scope)]
        if q and q.strip():
            term = q.strip()[:200]
            like = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append(or_(Document.search_tsv.op("@@")(func.plainto_tsquery("simple", term)),
                             Document.title.ilike(like, escape="\\"),
                             Document.registration_number.ilike(like, escape="\\")))
        if status is not None:
            where.append(Document.status == status)
        if type_code:
            where.append(DocumentType.code == type_code)
        if org_unit_id is not None:
            where.append(Document.org_unit_id == org_unit_id)
        if needs_review is not None:
            where.append(pending if needs_review else ~pending)
        if mine:
            where.append(Document.author_id == scope.user_id)

        base = (select(Document.id)
                .join(DocumentType, DocumentType.id == Document.document_type_id)
                .where(*where))
        stmt = (
            select(Document, DocumentType.code, DocumentType.name_uk, OrgUnit.name_uk.label("unit_name"),
                   User.first_name, User.last_name, ner_conf.label("ner_conf"), pending.label("pending"),
                   open_findings.label("open_findings"), verdict.label("verdict"))
            .join(DocumentType, DocumentType.id == Document.document_type_id)
            .join(OrgUnit, OrgUnit.id == Document.org_unit_id)
            .join(User, User.id == Document.author_id)
            .where(*where)
            .order_by(_ORDER[sort], Document.id)
            .limit(limit).offset(offset)
        )
        async with self.sessionmaker() as s:
            total = await s.scalar(select(func.count()).select_from(base.subquery())) or 0
            rows = (await s.execute(stmt)).all()
        items = [
            DocumentListItem(
                id=d.id, title=d.title, document_type_code=code, document_type_name=tname,
                status=d.status, direction=d.direction, access_level=d.access_level, org_unit_id=d.org_unit_id,
                org_unit_name=unit_name, author_name=display_name(first, last),
                registration_number=d.registration_number, registration_date=d.registration_date,
                created_at=d.created_at, updated_at=d.updated_at, ai_confidence=ner,
                needs_review=bool(pend), open_findings=int(open_n or 0), compliance_verdict=verd,
            )
            for d, code, tname, unit_name, first, last, ner, pend, open_n, verd in rows
        ]
        return DocumentPage(items=items, total=total, limit=limit, offset=offset)

    # --- картка ---------------------------------------------------------------------------

    async def card(self, scope: AccessScope, facts: DocumentFacts, *, request_id: str | None) -> DocumentCard:
        async with self.sessionmaker() as s, s.begin():
            doc = await s.get(Document, facts.id)
            assert doc is not None
            dtype = await s.get(DocumentType, doc.document_type_id)
            unit = await s.get(OrgUnit, doc.org_unit_id)
            assert dtype is not None and unit is not None
            versions = list((await s.scalars(select(DocumentVersion).where(DocumentVersion.document_id == doc.id)
                                             .order_by(DocumentVersion.version_number.desc()))).all())
            extraction = await self._extraction(s, doc.current_version_id)
            compliance = await self._compliance(s, doc.current_version_id)
            route = await self._route(s, doc.id)
            signatures = list((await s.scalars(
                select(Signature).join(DocumentVersion, DocumentVersion.id == Signature.document_version_id)
                .where(DocumentVersion.document_id == doc.id).order_by(Signature.signed_at))).all())
            history = list((await s.scalars(select(DocumentStatusHistory)
                                            .where(DocumentStatusHistory.document_id == doc.id)
                                            .order_by(DocumentStatusHistory.changed_at))).all())

            people: set[uuid.UUID | None] = {doc.author_id, *(v.created_by_id for v in versions),
                                             *(sg.signer_id for sg in signatures),
                                             *(h.changed_by_id for h in history)}
            if extraction:
                people |= {f.reviewed_by_id for f in extraction[1]}
            if compliance:
                people |= {f.resolved_by_id for f in compliance[2]}
            if route:
                people |= {route[0].initiated_by_id, *(st.assignee_id for st in route[1]),
                           *(st.original_assignee_id for st in route[1])}
            names = await _names(s, people)

            # Перегляд картки з персональними даними — подія журналу аудиту (ЗУ «Про захист ПД»).
            await set_actor(s, scope.user_id, request_id)
            await audit_event(s, "document.view", "documents", doc.id, doc.id,
                              {"access_level": doc.access_level.value})

        now = datetime.now(timezone.utc)
        my_step = None
        route_out = None
        if route:
            r, steps, tmpl_name = route
            my_step = next((st.id for st in steps if st.assignee_id == scope.user_id
                            and st.status is StepStatus.ACTIVE), None)
            route_out = RouteOut(
                id=r.id, status=r.status, document_version_id=r.document_version_id, template_name=tmpl_name,
                current_stage=r.current_stage, initiated_by=_person(r.initiated_by_id, names),  # type: ignore[arg-type]
                started_at=r.started_at, completed_at=r.completed_at, cancel_reason=r.cancel_reason,
                steps=[StepOut(
                    id=st.id, stage_no=st.stage_no, ordinal=st.ordinal, step_kind=st.step_kind,
                    assignee=_person(st.assignee_id, names),  # type: ignore[arg-type]
                    original_assignee=_person(st.original_assignee_id, names), acting_role=st.acting_role,
                    status=st.status, is_mandatory=st.is_mandatory, requires_kep=st.requires_kep,
                    activated_at=st.activated_at, due_at=st.due_at, decided_at=st.decided_at,
                    decision_comment=st.decision_comment,
                    is_overdue=st.status is StepStatus.ACTIVE and st.due_at is not None and st.due_at < now,
                ) for st in steps],
            )

        route_open = route_out is not None and route_out.status in (RouteStatus.DRAFT, RouteStatus.ACTIVE)
        has_text = {v.id: bool(v.extracted_text and v.extracted_text.strip()) for v in versions}
        current_has_text = bool(doc.current_version_id and has_text.get(doc.current_version_id))
        return DocumentCard(
            id=doc.id, title=doc.title, summary=doc.summary,
            document_type=DocumentTypeOut(id=dtype.id, code=dtype.code, name=dtype.name_uk,
                                          requires_kep=dtype.requires_kep),
            status=doc.status, direction=doc.direction, access_level=doc.access_level,
            org_unit=OrgUnitOut(id=unit.id, parent_id=unit.parent_id, kind=unit.kind, code=unit.code,
                                name=unit.name_uk, short_name=unit.short_name_uk),
            author=_person(doc.author_id, names),  # type: ignore[arg-type]
            registration_number=doc.registration_number, registration_date=doc.registration_date,
            correspondent_name=doc.correspondent_name, correspondent_number=doc.correspondent_number,
            correspondent_date=doc.correspondent_date, due_date=doc.due_date, requisites=doc.requisites or {},
            created_at=doc.created_at, updated_at=doc.updated_at, row_version=doc.row_version,
            current_version_id=doc.current_version_id,
            versions=[VersionOut(
                id=v.id, version_number=v.version_number, source=v.source, file_name=v.file_name,
                mime_type=v.mime_type, size_bytes=v.size_bytes, content_sha256=v.content_sha256,
                page_count=v.page_count, created_at=v.created_at,
                created_by=_person(v.created_by_id, names),  # type: ignore[arg-type]
                is_locked=v.is_locked, change_note=v.change_note, has_text=has_text[v.id],
            ) for v in versions],
            extraction=self._extraction_out(extraction, names) if extraction else None,
            compliance=self._compliance_out(compliance, names) if compliance else None,
            route=route_out,
            signatures=[SignatureOut(
                id=sg.id, document_version_id=sg.document_version_id, approval_step_id=sg.approval_step_id,
                signer=_person(sg.signer_id, names),  # type: ignore[arg-type]
                signature_format=sg.signature_format, provider=sg.provider, cert_subject_cn=sg.cert_subject_cn,
                cert_issuer=sg.cert_issuer, cert_serial=sg.cert_serial, signed_at=sg.signed_at,
                validation_status=sg.validation_status, validated_at=sg.validated_at,
            ) for sg in signatures],
            history=[StatusChangeOut(
                from_status=h.from_status, to_status=h.to_status, changed_by=_person(h.changed_by_id, names),
                actor_type=h.actor_type.value, reason=h.reason, changed_at=h.changed_at,
            ) for h in history],
            permissions=CardPermissions(
                review=can_review(scope, facts),
                check_compliance=can_review(scope, facts) and current_has_text,
                start_route=can_start_route(scope, facts) and not route_open,
                my_active_step_id=my_step,
            ),
        )

    async def _extraction(self, s: AsyncSession, version_id: uuid.UUID | None
                          ) -> tuple[AiRun, list[AiExtractedField], AiModel | None, AiRun | None] | None:
        if version_id is None:
            return None
        run = await s.scalar(select(AiRun).where(AiRun.document_version_id == version_id,
                                                 AiRun.task_type == AiTaskType.NER_EXTRACTION)
                             .order_by(AiRun.queued_at.desc()).limit(1))
        if run is None:
            return None
        fields = list((await s.scalars(select(AiExtractedField).where(AiExtractedField.ai_run_id == run.id)
                                       .order_by(AiExtractedField.created_at, AiExtractedField.field_key))).all())
        model = await s.get(AiModel, run.model_id)
        ocr = await s.scalar(select(AiRun).where(AiRun.document_version_id == version_id,
                                                 AiRun.task_type == AiTaskType.OCR)
                             .order_by(AiRun.queued_at.desc()).limit(1))
        return run, fields, model, ocr

    @staticmethod
    def _extraction_out(x: tuple[AiRun, list[AiExtractedField], AiModel | None, AiRun | None],
                        names: dict[uuid.UUID, str]) -> ExtractionOut:
        run, fields, model, ocr = x
        order = list(FIELD_LABELS_UK)
        fields = sorted(fields, key=lambda f: order.index(f.field_key) if f.field_key in order else 99)
        warnings = [str(w) for w in ((run.output or {}).get("warnings") or [])]
        return ExtractionOut(
            run=_run_out(run, model, warnings),
            fields=[ExtractedFieldOut(
                id=f.id, key=f.field_key, label=FIELD_LABELS_UK.get(f.field_key, f.field_key),
                extracted_value=f.extracted_value, validated_value=f.validated_value, confidence=f.confidence,
                requires_human_review=f.requires_human_review, review_status=f.human_review_status,
                reviewed_by=_person(f.reviewed_by_id, names), reviewed_at=f.reviewed_at,
                review_comment=f.review_comment, issues=_issues(run.output, f.field_key), page_no=f.page_no,
            ) for f in fields],
            ocr=ocr.output if ocr else None,
            ocr_confidence=ocr.confidence if ocr else None,
        )

    async def _compliance(self, s: AsyncSession, version_id: uuid.UUID | None
                          ) -> tuple[ComplianceCheck, AiRun, list[ComplianceFinding],
                                     dict[uuid.UUID, list[tuple[ComplianceCitation, RegulatoryChunk, RegulatorySource]]],
                                     AiModel | None] | None:
        if version_id is None:
            return None
        check = await s.scalar(select(ComplianceCheck).where(ComplianceCheck.document_version_id == version_id)
                               .order_by(ComplianceCheck.created_at.desc()).limit(1))
        if check is None:
            return None
        run = await s.get(AiRun, check.ai_run_id)
        assert run is not None
        model = await s.get(AiModel, run.model_id)
        findings = list((await s.scalars(select(ComplianceFinding).where(ComplianceFinding.check_id == check.id)
                                         .order_by(ComplianceFinding.ordinal))).all())
        cites: dict[uuid.UUID, list[tuple[ComplianceCitation, RegulatoryChunk, RegulatorySource]]] = {}
        if findings:
            rows = (await s.execute(
                select(ComplianceCitation, RegulatoryChunk, RegulatorySource)
                .join(RegulatoryChunk, RegulatoryChunk.id == ComplianceCitation.chunk_id)
                .join(RegulatorySource, RegulatorySource.id == RegulatoryChunk.source_id)
                .where(ComplianceCitation.finding_id.in_([f.id for f in findings]))
                .order_by(ComplianceCitation.rank)
            )).all()
            for c, ch, src in rows:
                cites.setdefault(c.finding_id, []).append((c, ch, src))
        return check, run, findings, cites, model

    @staticmethod
    def _compliance_out(x: Any, names: dict[uuid.UUID, str]) -> ComplianceOut:
        check, run, findings, cites, model = x
        out = run.output or {}
        return ComplianceOut(
            check_id=check.id, run=_run_out(run, model, [str(w) for w in out.get("warnings") or []]),
            document_version_id=check.document_version_id, verdict=check.verdict, summary=check.summary,
            regulations_as_of=check.regulations_as_of, sources_considered=check.sources_considered,
            created_at=check.created_at, review_reasons=[str(r) for r in out.get("review_reasons") or []],
            findings=[FindingOut(
                id=f.id, ordinal=f.ordinal, severity=f.severity, category=f.category, title=f.title,
                explanation=f.explanation, recommendation=f.recommendation, fragment_text=f.fragment_text,
                confidence=f.confidence, requires_human_review=f.requires_human_review, status=f.status,
                resolved_by=_person(f.resolved_by_id, names), resolved_at=f.resolved_at,
                resolution_comment=f.resolution_comment,
                citations=[CitationOut(
                    rank=c.rank, citation_label=f"{src.title}, {ch.section_ref}" if ch.section_ref else src.title,
                    source_title=src.title, section_ref=ch.section_ref, quoted_text=c.quoted_text,
                    quote_verified=c.quote_verified, source_status=c.source_status_snapshot,
                ) for c, ch, src in cites.get(f.id, [])],
            ) for f in findings],
        )

    async def _route(self, s: AsyncSession, document_id: uuid.UUID
                     ) -> tuple[ApprovalRoute, list[ApprovalStep], str | None] | None:
        route = await s.scalar(select(ApprovalRoute).where(ApprovalRoute.document_id == document_id)
                               .order_by(ApprovalRoute.created_at.desc()).limit(1))
        if route is None:
            return None
        steps = list((await s.scalars(select(ApprovalStep).where(ApprovalStep.route_id == route.id)
                                      .order_by(ApprovalStep.stage_no, ApprovalStep.ordinal))).all())
        tmpl = await s.scalar(select(RouteTemplate.name_uk).where(RouteTemplate.id == route.template_id)) \
            if route.template_id else None
        return route, steps, tmpl

    # --- черги ------------------------------------------------------------------------------

    async def inbox(self, user_id: uuid.UUID) -> list[InboxItem]:
        author = aliased(User)
        original = aliased(User)
        stmt = (
            select(ApprovalStep, ApprovalRoute.document_id, Document.title, Document.status,
                   DocumentType.name_uk, OrgUnit.name_uk, author.first_name, author.last_name,
                   original.first_name.label("of"), original.last_name.label("ol"))
            .join(ApprovalRoute, ApprovalRoute.id == ApprovalStep.route_id)
            .join(Document, Document.id == ApprovalRoute.document_id)
            .join(DocumentType, DocumentType.id == Document.document_type_id)
            .join(OrgUnit, OrgUnit.id == Document.org_unit_id)
            .join(author, author.id == Document.author_id)
            .outerjoin(original, original.id == ApprovalStep.original_assignee_id)
            .where(ApprovalStep.assignee_id == user_id, ApprovalStep.status == StepStatus.ACTIVE,
                   Document.deleted_at.is_(None))
            .order_by(ApprovalStep.due_at.asc().nulls_last(), ApprovalStep.activated_at)
        )
        now = datetime.now(timezone.utc)
        async with self.sessionmaker() as s:
            rows = (await s.execute(stmt)).all()
        return [InboxItem(
            step_id=st.id, route_id=st.route_id, document_id=doc_id, document_title=title, document_status=dstatus,
            document_type_name=tname, org_unit_name=uname, author_name=display_name(af, al),
            step_kind=st.step_kind, stage_no=st.stage_no, acting_role=st.acting_role,
            requires_kep=st.requires_kep, activated_at=st.activated_at, due_at=st.due_at,
            is_overdue=st.due_at is not None and st.due_at < now,
            delegated_from=display_name(of, ol) if of else None,
        ) for st, doc_id, title, dstatus, tname, uname, af, al, of, ol in rows]

    async def review_queue(self, scope: AccessScope, limit: int = 100) -> list[ReviewQueueItem]:
        pending_fields = (select(func.count(AiExtractedField.id))
                          .where(AiExtractedField.ai_run_id == AiRun.id,
                                 AiExtractedField.human_review_status == HumanReviewStatus.PENDING)
                          .correlate(AiRun).scalar_subquery())
        stmt = (
            select(AiRun, Document.title, OrgUnit.name_uk, pending_fields.label("pf"))
            .join(Document, and_(Document.id == AiRun.document_id,
                                 Document.current_version_id == AiRun.document_version_id))
            .join(OrgUnit, OrgUnit.id == Document.org_unit_id)
            .where(review_clause(scope),
                   AiRun.task_type.in_((AiTaskType.NER_EXTRACTION, AiTaskType.COMPLIANCE_CHECK)),
                   or_(AiRun.human_review_status == HumanReviewStatus.PENDING, pending_fields > 0),
                   Document.status.in_((DocumentStatus.DRAFT, DocumentStatus.AI_REVIEW,
                                        DocumentStatus.REVISION_REQUIRED, DocumentStatus.ON_APPROVAL)))
            .order_by(AiRun.queued_at)
            .limit(limit)
        )
        async with self.sessionmaker() as s:
            rows = (await s.execute(stmt)).all()
        return [ReviewQueueItem(
            document_id=run.document_id, document_title=title, org_unit_name=uname,
            task="requisites" if run.task_type is AiTaskType.NER_EXTRACTION else "compliance",
            ai_run_id=run.id, confidence=run.confidence, pending_fields=int(pf or 0), queued_at=run.queued_at,
        ) for run, title, uname, pf in rows]
