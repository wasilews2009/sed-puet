"""Прикладний сервіс маршрутизації погодження (use cases).

Кожна операція — одна транзакція PostgreSQL із заданим актором (`sed.actor_*`), щоб тригери
історії та аудиту записали, хто що зробив. Порядок блокувань завжди документ → маршрут, тож
одночасні візи на одному маршруті серіалізуються без взаємних блокувань.

Автоматичні переходи (активація наступного етапу, пропуск кроку, завершення маршруту)
пишуться від актора `system`, рішення по кроку — від людини, що його прийняла.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from sqlalchemy import exists, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from sed_db.enums import (
    LABELS_UK,
    AssigneeRule,
    DocumentStatus,
    FindingSeverity,
    FindingStatus,
    RouteOrigin,
    RouteStatus,
    SignatureProvider,
    SignatureValidation,
    StepKind,
    StepStatus,
    UserRole,
)
from sed_db.models import (
    ApprovalRoute,
    ApprovalStep,
    ComplianceCheck,
    ComplianceFinding,
    Document,
    DocumentType,
    DocumentVersion,
    RouteTemplate,
    Signature,
    User,
    UserRoleAssignment,
)
from sed_db.schemas import StepDecision
from sed_db.seeds import UNSORTED_TYPE_CODE

from . import views
from .adapters.sql_directory import SqlOrgDirectory
from .domain import state_machine as sm
from .domain.errors import (
    AccessDenied,
    AssigneeUnresolved,
    BlockingFindings,
    InvalidState,
    NotFound,
    SignatureRejected,
    TemplateNotSelected,
)
from .domain.models import DocumentContext, PlannedStep, StepRule, TemplateRef
from .domain.resolution import AssigneeResolver
from .domain.templates import StepExtras, TemplateCatalog
from .kep.models import VerificationResult
from .kep.verifier import CadesVerifier
from .kep.certinfo import LEVEL_LABELS_UK, CertLevel
from .ports import DocumentContentReader, SignatureStore

log = logging.getLogger(__name__)

STARTABLE = (DocumentStatus.DRAFT, DocumentStatus.REVISION_REQUIRED)
INITIATOR_ROLES = (UserRole.STAFF, UserRole.UNIT_HEAD, UserRole.DEPARTMENT_HEAD, UserRole.INSTITUTE_DIRECTOR)


@dataclass(frozen=True)
class Caller:
    """Автентифікований виклик (з JWT Keycloak)."""

    keycloak_sub: uuid.UUID
    roles: frozenset[str] = frozenset()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _derived_title(rule: AssigneeRule, role: UserRole | None) -> str:
    if role is not None:
        return LABELS_UK[role]
    return {AssigneeRule.AUTHOR_UNIT_HEAD: "Керівник підрозділу автора",
            AssigneeRule.UNIT_HEAD: "Керівник підрозділу"}.get(rule, "Учасник погодження")


@dataclass
class _Loaded:
    """Заблокований контекст кроку/маршруту в межах транзакції."""

    doc_orm: Document
    doc: DocumentContext
    route_orm: ApprovalRoute
    step_orms: dict[uuid.UUID, ApprovalStep]
    state: sm.RouteState
    template: TemplateRef | None


def _enforce_level(result: VerificationResult, accepted: tuple[str, ...]) -> VerificationResult:
    """Політика кроку: внутрішній ключ АІ-СЕД ПУЕТ — лише там, де шаблон це дозволяє.
    Імітаційний КНЕДП (лише dev/test) рахується як КЕП."""
    level = CertLevel.QUALIFIED if result.level is CertLevel.MOCK else result.level
    if level is not None and level.value in accepted:
        return result
    if level is CertLevel.INTERNAL:
        reason = ("Цей крок вимагає кваліфікованого КЕП (Дія, ПриватБанк або інший КНЕДП): "
                  "внутрішній ключ АІ-СЕД ПУЕТ тут не приймається")
    else:
        reason = "Рівень підпису не дозволено для цього кроку"
    return replace(result, status=SignatureValidation.INVALID, reasons=(reason, *result.reasons),
                   checks={**result.checks, "level_policy": f"{result.level} ∉ {list(accepted)}"})


class ApprovalService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        catalog: TemplateCatalog,
        verifier: CadesVerifier,
        signature_store: SignatureStore,
        *,
        clock: Callable[[], datetime] = utcnow,
        block_on_critical_findings: bool = True,
        ai_service_roles: frozenset[str] = frozenset({"sed-ai-service"}),
        content_reader: DocumentContentReader | None = None,
    ) -> None:
        self.content_reader = content_reader
        self.sessionmaker = sessionmaker
        self.catalog = catalog
        self.verifier = verifier
        self.store = signature_store
        self.clock = clock
        self.block_on_critical_findings = block_on_critical_findings
        self.ai_service_roles = ai_service_roles

    # ------------------------------------------------------------------ infrastructure

    @asynccontextmanager
    async def _tx(self) -> AsyncIterator[AsyncSession]:
        async with self.sessionmaker() as s, s.begin():
            yield s

    @staticmethod
    async def _act(s: AsyncSession, user_id: uuid.UUID | None, kind: str = "user", reason: str | None = None) -> None:
        await s.execute(
            text("SELECT set_config('sed.actor_user_id', :u, true), set_config('sed.actor_type', :k, true), "
                 "set_config('sed.change_reason', :r, true)"),
            {"u": str(user_id) if user_id else "", "k": kind, "r": reason or ""})

    async def _user(self, s: AsyncSession, caller: Caller) -> User:
        if caller.roles & self.ai_service_roles:
            # Людина в контурі: ШІ не візує й не підписує (другий рубіж — тригер у БД).
            raise AccessDenied("Сервісний обліковий запис ШІ не може брати участь у погодженні")
        user = await s.scalar(select(User).where(User.keycloak_sub == caller.keycloak_sub))
        if user is None or not user.is_active:
            raise AccessDenied("Користувача не знайдено або його деактивовано")
        await self._act(s, user.id)
        return user

    async def _document(self, s: AsyncSession, document_id: uuid.UUID, *, lock: bool) -> tuple[Document, DocumentContext]:
        q = select(Document).where(Document.id == document_id, Document.deleted_at.is_(None))
        doc = await s.scalar(q.with_for_update() if lock else q)
        if doc is None:
            raise NotFound("Документ не знайдено")
        type_code = await s.scalar(select(DocumentType.code).where(DocumentType.id == doc.document_type_id))
        sha = await s.scalar(select(DocumentVersion.content_sha256)
                             .where(DocumentVersion.id == doc.current_version_id)) if doc.current_version_id else None
        return doc, DocumentContext(
            id=doc.id, document_type_id=doc.document_type_id, document_type_code=type_code,
            org_unit_id=doc.org_unit_id, author_id=doc.author_id, status=doc.status,
            current_version_id=doc.current_version_id, current_version_sha256=sha, title=doc.title)

    async def _ensure_can_initiate(self, s: AsyncSession, user: User, doc: DocumentContext) -> None:
        if user.id == doc.author_id:
            return
        today = self.clock().date()
        clerk = await s.scalar(select(exists().where(
            UserRoleAssignment.user_id == user.id, UserRoleAssignment.org_unit_id == doc.org_unit_id,
            UserRoleAssignment.role.in_(INITIATOR_ROLES), UserRoleAssignment.valid_from <= today,
            or_(UserRoleAssignment.valid_to.is_(None), UserRoleAssignment.valid_to > today))))
        if not clerk:
            raise AccessDenied("Запустити погодження може автор документа або працівник його підрозділу")

    # ------------------------------------------------------------------ templates

    def _to_ref(self, tpl: RouteTemplate) -> TemplateRef:
        spec = self.catalog.get(tpl.code)
        extras: dict[tuple[int, int], StepExtras] = {}
        if spec is not None:
            numbered = {(st, o): sp for st, o, sp in spec.numbered_steps()}
            same = len(numbered) == len(tpl.steps) and all(
                (sp := numbered.get((d.stage_no, d.ordinal))) is not None
                and (sp.kind, sp.rule, sp.role) == (d.step_kind, d.assignee_rule, d.role) for d in tpl.steps)
            if same:
                extras = {k: v.extras for k, v in numbered.items()}
            else:
                log.warning("Шаблон %s v%s не збігається з каталогом: назви посад виведено з ролей",
                            tpl.code, tpl.version)
        rules = tuple(
            StepRule(stage_no=d.stage_no, ordinal=d.ordinal, step_kind=d.step_kind, rule=d.assignee_rule,
                     extras=extras.get((d.stage_no, d.ordinal)) or StepExtras(
                         title_uk=_derived_title(d.assignee_rule, d.role)),
                     template_step_id=d.id, role=d.role, unit_id=d.assignee_org_unit_id,
                     position_code=d.position_code, user_id=d.assignee_user_id, deadline_hours=d.deadline_hours,
                     is_mandatory=d.is_mandatory, requires_kep=d.requires_kep)
            for d in tpl.steps)
        return TemplateRef(tpl.id, tpl.code, tpl.version, tpl.name_uk, tpl.org_unit_id, rules)

    async def _template_by_id(self, s: AsyncSession, template_id: uuid.UUID | None) -> TemplateRef | None:
        if template_id is None:
            return None
        tpl = await s.scalar(select(RouteTemplate).options(selectinload(RouteTemplate.steps))
                             .where(RouteTemplate.id == template_id))
        return self._to_ref(tpl) if tpl else None

    async def _select_template(self, s: AsyncSession, doc: DocumentContext, code: str | None) -> TemplateRef:
        q = (select(RouteTemplate).options(selectinload(RouteTemplate.steps))
             .where(RouteTemplate.document_type_id == doc.document_type_id, RouteTemplate.is_active))
        if code:
            q = q.where(RouteTemplate.code == code)
        rows = list(await s.scalars(q))
        chain = [u.id for u in await SqlOrgDirectory(s).unit_chain(doc.org_unit_id)]

        def specificity(t: RouteTemplate) -> int | None:
            if t.org_unit_id is None:
                return len(chain)
            return chain.index(t.org_unit_id) if t.org_unit_id in chain else None

        scoped = [(sp, t) for t in rows if (sp := specificity(t)) is not None]
        if not scoped:
            raise TemplateNotSelected(
                f"Для виду документа «{doc.document_type_code}» немає чинного шаблону маршруту"
                + (f" з кодом {code}" if code else ""))
        best = min(sp for sp, _ in scoped)
        top = [t for sp, t in scoped if sp == best]
        if len({t.code for t in top}) > 1:
            raise TemplateNotSelected(
                "Для цього виду документа є кілька маршрутів — оберіть template_code",
                candidates=[{"code": t.code, "name": t.name_uk} for t in top])
        return self._to_ref(max(top, key=lambda t: t.version))

    # ------------------------------------------------------------------ planning

    async def _plan(self, s: AsyncSession, doc: DocumentContext, tpl: TemplateRef,
                    overrides: list[views.AssigneeOverride]) -> list[PlannedStep]:
        by_key = {(o.stage_no, o.ordinal): o.user_id for o in overrides}
        unknown = set(by_key) - {(r.stage_no, r.ordinal) for r in tpl.rules}
        if unknown:
            raise AssigneeUnresolved("Перепризначення вказано для неіснуючих кроків", steps=sorted(unknown))
        resolver = AssigneeResolver(SqlOrgDirectory(s))
        today = self.clock().date()
        return [await resolver.plan_step(r, doc, today, by_key.get((r.stage_no, r.ordinal))) for r in tpl.rules]

    def _check_startable(self, doc: DocumentContext) -> None:
        if doc.document_type_code == UNSORTED_TYPE_CODE:
            raise InvalidState("Вид документа не визначено: уточніть його перед погодженням")
        if doc.current_version_id is None:
            raise InvalidState("Документ не має жодної версії")
        if doc.status not in STARTABLE:
            raise InvalidState(f"Документ у статусі «{LABELS_UK.get(doc.status, doc.status.value)}» "
                               "не можна відправити на погодження")

    async def _check_findings(self, s: AsyncSession, user: User, doc: DocumentContext, version_id: uuid.UUID,
                              acknowledged: bool) -> None:
        open_critical = await s.scalar(
            select(func.count()).select_from(ComplianceFinding)
            .join(ComplianceCheck, ComplianceCheck.id == ComplianceFinding.check_id)
            .where(ComplianceCheck.document_version_id == version_id,
                   ComplianceFinding.status == FindingStatus.OPEN,
                   ComplianceFinding.severity == FindingSeverity.CRITICAL))
        if not open_critical or not self.block_on_critical_findings:
            return
        if not acknowledged:
            raise BlockingFindings(
                f"На версію є {open_critical} відкритих критичних зауважень комплаєнс-аудиту: "
                "опрацюйте їх або підтвердіть запуск попри зауваження", count=open_critical)
        await s.execute(text("SELECT sed_audit_event('route.findings_acknowledged', 'documents', :d, :d, "
                             "CAST(:det AS jsonb))"),
                        {"d": doc.id, "det": json.dumps({"open_critical": open_critical, "by": str(user.id)})})

    async def preview(self, caller: Caller, document_id: uuid.UUID, req: views.RouteRequest) -> views.RoutePlanView:
        """Хто має завізувати документ — без збереження."""
        async with self._tx() as s:
            user = await self._user(s, caller)
            _, doc = await self._document(s, document_id, lock=False)
            await self._ensure_can_initiate(s, user, doc)
            self._check_startable(doc)
            tpl = await self._select_template(s, doc, req.template_code)
            plan = await self._plan(s, doc, tpl, req.assignee_overrides)
            return views.RoutePlanView(
                document_id=doc.id, document_version_id=doc.current_version_id,  # type: ignore[arg-type]
                template_code=tpl.code, template_version=tpl.version, template_name=tpl.name_uk,
                steps=[views.PlannedStepView(
                    stage_no=p.rule.stage_no, ordinal=p.rule.ordinal, title_uk=p.rule.extras.title_uk,
                    step_kind=p.rule.step_kind, assignee_id=p.assignee.user_id,
                    assignee_name=p.assignee.display_name, acting_role=p.acting_role,
                    requires_kep=p.rule.requires_kep, is_mandatory=p.rule.is_mandatory,
                    deadline_hours=p.rule.deadline_hours, will_skip=p.will_skip, skip_reason=p.skip_reason)
                    for p in plan])

    # ------------------------------------------------------------------ route lifecycle

    async def create_route(self, caller: Caller, document_id: uuid.UUID, req: views.RouteCreate) -> views.RouteView:
        async with self._tx() as s:
            user = await self._user(s, caller)
            doc_orm, doc = await self._document(s, document_id, lock=True)
            await self._ensure_can_initiate(s, user, doc)
            self._check_startable(doc)
            if await s.scalar(select(exists().where(ApprovalRoute.document_id == doc.id,
                                                    ApprovalRoute.status.in_((RouteStatus.DRAFT, RouteStatus.ACTIVE))))):
                raise InvalidState("Документ уже має незавершений маршрут погодження")
            tpl = await self._select_template(s, doc, req.template_code)
            plan = await self._plan(s, doc, tpl, req.assignee_overrides)

            route = ApprovalRoute(document_id=doc.id, document_version_id=doc.current_version_id,
                                  template_id=tpl.id, origin=RouteOrigin.TEMPLATE, status=RouteStatus.DRAFT,
                                  initiated_by_id=user.id)
            s.add(route)
            await s.flush()
            s.add_all(ApprovalStep(
                route_id=route.id, template_step_id=p.rule.template_step_id, stage_no=p.rule.stage_no,
                ordinal=p.rule.ordinal, step_kind=p.rule.step_kind, assignee_id=p.assignee.user_id,
                acting_role=p.acting_role, is_mandatory=p.rule.is_mandatory, requires_kep=p.rule.requires_kep)
                for p in plan)
            await s.flush()
            warnings: list[str] = []
            if req.start:
                loaded = await self._load(s, route.id, lock=False, doc_pair=(doc_orm, doc))
                warnings = await self._start(s, user, loaded, req.acknowledge_findings)
            return await self._view(s, route.id, warnings)

    async def start_route(self, caller: Caller, route_id: uuid.UUID, req: views.RouteStart) -> views.RouteView:
        async with self._tx() as s:
            user = await self._user(s, caller)
            loaded = await self._load(s, route_id, lock=True)
            if user.id not in (loaded.route_orm.initiated_by_id, loaded.doc.author_id):
                raise AccessDenied("Запустити маршрут може ініціатор або автор документа")
            self._check_startable(loaded.doc)
            if loaded.doc.current_version_id != loaded.route_orm.document_version_id:
                raise InvalidState("Після створення маршруту з'явилася нова версія документа: створіть маршрут заново")
            warnings = await self._start(s, user, loaded, req.acknowledge_findings)
            return await self._view(s, route_id, warnings)

    async def _start(self, s: AsyncSession, user: User, ld: _Loaded, acknowledged: bool) -> list[str]:
        await self._check_findings(s, user, ld.doc, ld.route_orm.document_version_id, acknowledged)
        now = self.clock()
        sm.start(ld.state, now)
        await self._act(s, user.id, reason="Відправлено на погодження")
        ld.doc_orm.status = DocumentStatus.ON_APPROVAL
        self._write_route(ld)
        await s.flush()  # маршрут active → тригер блокує версію від змін
        return await self._advance(s, ld, now)

    async def cancel_route(self, caller: Caller, route_id: uuid.UUID, req: views.RouteCancel) -> views.RouteView:
        async with self._tx() as s:
            user = await self._user(s, caller)
            ld = await self._load(s, route_id, lock=True)
            if user.id not in (ld.route_orm.initiated_by_id, ld.doc.author_id):
                raise AccessDenied("Скасувати маршрут може ініціатор або автор документа")
            if ld.state.status not in (RouteStatus.DRAFT, RouteStatus.ACTIVE):
                raise InvalidState(f"Маршрут уже завершено ({ld.state.status.value})")
            now = self.clock()
            sm.finish(ld.state, RouteStatus.CANCELLED, now, req.reason)
            await self._act(s, user.id, reason=f"Маршрут скасовано: {req.reason}")
            self._write_route(ld)
            self._write_steps(ld)
            if ld.doc_orm.status is DocumentStatus.ON_APPROVAL:
                ld.doc_orm.status = DocumentStatus.DRAFT
            await s.flush()
            return await self._view(s, route_id)

    # ------------------------------------------------------------------ decisions

    async def decide(self, caller: Caller, step_id: uuid.UUID, decision: StepDecision) -> views.RouteView:
        """Рішення без КЕП: відхилення або крок, що не вимагає підпису."""
        async with self._tx() as s:
            user = await self._user(s, caller)
            ld = await self._load_for_step(s, step_id)
            step = ld.state.step(step_id)
            sm.ensure_can_decide(ld.state, step, user.id)
            self._ensure_same_version(ld)
            if step.requires_kep and decision.decision is not StepStatus.REJECTED:
                raise InvalidState("Цей крок погоджується лише з накладанням КЕП: використайте підписання")
            now = self.clock()
            sm.record_decision(step, decision.decision, decision.comment, now)
            self._write_steps(ld)
            await s.flush()
            warnings = await self._advance(s, ld, now)
            return await self._view(s, ld.route_orm.id, warnings)

    async def signing_request(self, caller: Caller, step_id: uuid.UUID) -> views.SigningRequestView:
        async with self._tx() as s:
            user = await self._user(s, caller)
            ld = await self._load_for_step(s, step_id, lock=False)
            step = ld.state.step(step_id)
            sm.ensure_can_decide(ld.state, step, user.id)
            if not step.requires_kep:
                raise InvalidState("Крок не потребує КЕП")
            ver = await s.get(DocumentVersion, ld.route_orm.document_version_id)
            levels = self._levels(ld, step_id)
            return views.SigningRequestView(
                step_id=step_id, document_id=ld.doc.id, document_version_id=ver.id, file_name=ver.file_name,
                content_sha256=ver.content_sha256, not_before=step.activated_at,
                accepted_levels=list(levels), accepted_levels_uk=[LEVEL_LABELS_UK[CertLevel(x)] for x in levels])

    async def sign(self, caller: Caller, step_id: uuid.UUID, req: views.SignRequest) -> views.RouteView:
        """Віза з КЕП. Наступний етап активується лише після валідного підпису."""
        try:
            container = base64.b64decode(req.signature_base64, validate=True)
        except (binascii.Error, ValueError):
            raise SignatureRejected("Підпис має бути в base64 (DER-контейнер CAdES)") from None

        # 1) Контекст і перевірка підпису — поза блокуванням (OCSP може відповідати секунди).
        async with self._tx() as s:
            user = await self._user(s, caller)
            ld = await self._load_for_step(s, step_id, lock=False)
            step = ld.state.step(step_id)
            sm.ensure_can_decide(ld.state, step, user.id)
            self._ensure_same_version(ld)
            if not step.requires_kep:
                raise InvalidState("Крок не потребує КЕП: використайте звичайне рішення")
            ver = await s.get(DocumentVersion, ld.route_orm.document_version_id)
            expected, activated_at, user_id = ver.content_sha256, step.activated_at, user.id
            doc_id, version_id = ld.doc.id, ver.id
            levels = self._levels(ld, step_id)
            loc = (ver.storage_bucket, ver.storage_key, ver.storage_version_id)
        now = self.clock()
        reader = self.content_reader

        async def content() -> bytes:  # лише для ДСТУ 4145: провайдер гешує сам файл
            assert reader is not None
            return await reader.read(*loc)

        result = await self.verifier.verify(container, expected_sha256=expected, signer_user_id=user_id,
                                            not_before=activated_at, now=now,
                                            content=content if reader is not None else None)
        if result.ok:
            result = _enforce_level(result, levels)
        if not result.ok:
            async with self._tx() as s:
                await self._act(s, user_id)
                await s.execute(
                    text("SELECT sed_audit_event('kep.signature_rejected', 'approval_steps', :st, :d, "
                         "CAST(:det AS jsonb))"),
                    {"st": step_id, "d": doc_id, "det": json.dumps(result.report(), ensure_ascii=False)})
            raise SignatureRejected(result.reasons[0], validation_status=result.status.value,
                                    reasons=list(result.reasons))

        # 2) Контейнер у сховище, далі одна транзакція: підпис → рішення → наступний етап.
        key = f"{doc_id}/{version_id}/{step_id}-{uuid.uuid4().hex[:12]}.p7s"
        cert = result.certificate
        assert cert is not None and result.signed_at is not None and result.signature_format is not None
        await self.store.put(key, container, {"document-id": str(doc_id), "step-id": str(step_id),
                                              "signer-id": str(user_id), "sha256": expected})
        try:
            async with self._tx() as s:
                user = await self._user(s, caller)
                ld = await self._load_for_step(s, step_id)
                step = ld.state.step(step_id)
                sm.ensure_can_decide(ld.state, step, user.id)  # стан міг змінитися, поки йшла перевірка
                self._ensure_same_version(ld)
                s.add(Signature(
                    document_version_id=version_id, signer_id=user.id, approval_step_id=step_id,
                    signature_format=result.signature_format, provider=SignatureProvider(req.provider),
                    storage_bucket=self.store.bucket, storage_key=key, signed_content_sha256=expected,
                    cert_serial=cert.serial_hex[:128], cert_issuer=cert.issuer[:512],
                    cert_subject_cn=cert.subject_cn[:512],
                    signer_tax_id_hmac=self.verifier.hasher(cert.tax_id)
                    if cert.tax_id and result.level is not CertLevel.INTERNAL else None,
                    cert_valid_from=cert.valid_from, cert_valid_to=cert.valid_to,
                    signed_at=result.signed_at if result.signed_at.tzinfo else result.signed_at.replace(tzinfo=timezone.utc),
                    validation_status=result.status, validation_report=result.report(), validated_at=now))
                await s.flush()  # тригер: геш = геш версії, підписант = учасник активного кроку
                sm.record_decision(step, req.decision, req.comment, now)
                self._write_steps(ld)
                await s.flush()
                warnings = await self._advance(s, ld, now)
                return await self._view(s, ld.route_orm.id, warnings)
        except BaseException:
            await self.store.delete(key)
            raise

    @staticmethod
    def _levels(ld: _Loaded, step_id: uuid.UUID) -> tuple[str, ...]:
        """Рівні підпису, які приймає крок (з каталогу шаблонів); за замовчуванням — лише КЕП."""
        tsid = ld.step_orms[step_id].template_step_id
        for r in ld.template.rules if ld.template else ():
            if tsid is not None and r.template_step_id == tsid:
                return tuple(r.extras.signature_levels)
        return ("qualified",)

    def _ensure_same_version(self, ld: _Loaded) -> None:
        if ld.doc.current_version_id != ld.route_orm.document_version_id:
            raise InvalidState("Документ має новішу версію, ніж та, що на погодженні: маршрут треба перезапустити")

    # ------------------------------------------------------------------ advancing

    async def _advance(self, s: AsyncSession, ld: _Loaded, now: datetime) -> list[str]:
        """Автоматичні переходи після рішення/запуску, доки маршрут не стане чекати на людину."""
        warnings: list[str] = []
        st = ld.state
        await self._act(s, None, "system")
        while True:
            nxt, stage = sm.evaluate(st)
            if nxt is sm.Next.WAIT:
                break
            if nxt is sm.Next.REJECT:
                sm.close_open_steps(st, stage, now, "Маршрут відхилено іншим учасником етапу")
                sm.finish(st, RouteStatus.REJECTED, now)
                await self._act(s, None, "system", "Відхилено під час погодження")
                ld.doc_orm.status = DocumentStatus.REVISION_REQUIRED
                break
            if st.current_stage is not None:
                sm.close_open_steps(st, st.current_stage, now, "Етап завершено без цього необов'язкового кроку")
            if nxt is sm.Next.COMPLETE:
                sm.finish(st, RouteStatus.COMPLETED, now)
                signed = any(x.step_kind is StepKind.SIGNING and x.status in sm.POSITIVE for x in st.steps)
                await self._act(s, None, "system", "Усі учасники маршруту погодили документ")
                ld.doc_orm.status = DocumentStatus.SIGNED if signed else DocumentStatus.APPROVED
                break
            await self._activate(s, ld, stage, now, warnings)  # type: ignore[arg-type]
        self._write_route(ld)
        self._write_steps(ld)
        await s.flush()
        return warnings

    async def _activate(self, s: AsyncSession, ld: _Loaded, stage: int, now: datetime, warnings: list[str]) -> None:
        st, doc = ld.state, ld.doc
        st.current_stage = stage
        rules = {r.template_step_id: r for r in (ld.template.rules if ld.template else ())}
        resolver = AssigneeResolver(SqlOrgDirectory(s))
        today = now.date()
        for step in st.stage_steps(stage):
            if step.status is not StepStatus.WAITING:
                continue
            rule = rules.get(step.template_step_id)
            assignee = step.assignee_id
            title = rule.extras.title_uk if rule else LABELS_UK.get(step.step_kind, "Крок")
            # Посада важливіша за людину: якщо за час маршруту змінився керівник — візує новий.
            if rule is not None and not await resolver.still_holds(rule, doc, assignee, today):
                try:
                    assignee = (await resolver.plan_step(rule, doc, today)).assignee.user_id
                    warnings.append(f"«{title}»: учасника змінено, бо попередній більше не обіймає посаду")
                except AssigneeUnresolved as exc:
                    warnings.append(f"«{title}»: {exc.message}; крок лишився за попереднім учасником")
            if rule is not None and rule.extras.skip_if_author and assignee == doc.author_id:
                step.set(assignee_id=assignee, status=StepStatus.SKIPPED, decided_at=now,
                         decision_comment="Пропущено: автор документа сам обіймає цю посаду")
                continue
            final, delegation = await resolver.effective_assignee(assignee, doc.document_type_id, now)
            if delegation is not None:
                step.set(assignee_id=final, original_assignee_id=assignee, delegation_id=delegation.id)
            else:
                step.set(assignee_id=assignee)
            hours = rule.deadline_hours if rule else None
            step.set(status=StepStatus.ACTIVE, activated_at=now,
                     due_at=now + timedelta(hours=hours) if hours else None)

    # ------------------------------------------------------------------ loading / writing

    async def _load(self, s: AsyncSession, route_id: uuid.UUID, *, lock: bool,
                    doc_pair: tuple[Document, DocumentContext] | None = None) -> _Loaded:
        doc_id = await s.scalar(select(ApprovalRoute.document_id).where(ApprovalRoute.id == route_id))
        if doc_id is None:
            raise NotFound("Маршрут не знайдено")
        doc_orm, doc = doc_pair or await self._document(s, doc_id, lock=lock)  # документ → маршрут
        q = select(ApprovalRoute).where(ApprovalRoute.id == route_id)
        route = await s.scalar(q.with_for_update() if lock else q)
        steps = list(await s.scalars(select(ApprovalStep).where(ApprovalStep.route_id == route_id)))
        state = sm.RouteState(
            id=route.id, document_id=route.document_id, document_version_id=route.document_version_id,
            status=route.status, initiated_by_id=route.initiated_by_id, current_stage=route.current_stage,
            started_at=route.started_at, completed_at=route.completed_at, cancel_reason=route.cancel_reason,
            template_id=route.template_id,
            steps=[sm.StepState(
                id=x.id, stage_no=x.stage_no, ordinal=x.ordinal, step_kind=x.step_kind, assignee_id=x.assignee_id,
                status=x.status, is_mandatory=x.is_mandatory, requires_kep=x.requires_kep,
                acting_role=x.acting_role, template_step_id=x.template_step_id,
                original_assignee_id=x.original_assignee_id, delegation_id=x.delegation_id,
                activated_at=x.activated_at, due_at=x.due_at, decided_at=x.decided_at,
                decision_comment=x.decision_comment) for x in steps])
        return _Loaded(doc_orm, doc, route, {x.id: x for x in steps}, state,
                       await self._template_by_id(s, route.template_id))

    async def _load_for_step(self, s: AsyncSession, step_id: uuid.UUID, *, lock: bool = True) -> _Loaded:
        route_id = await s.scalar(select(ApprovalStep.route_id).where(ApprovalStep.id == step_id))
        if route_id is None:
            raise NotFound("Крок погодження не знайдено")
        return await self._load(s, route_id, lock=lock)

    @staticmethod
    def _write_route(ld: _Loaded) -> None:
        r, st = ld.route_orm, ld.state
        r.status, r.current_stage, r.started_at = st.status, st.current_stage, st.started_at
        r.completed_at, r.cancel_reason = st.completed_at, st.cancel_reason

    @staticmethod
    def _write_steps(ld: _Loaded) -> None:
        for st in ld.state.steps:
            if not st.dirty:
                continue
            o = ld.step_orms[st.id]
            for f in ("assignee_id", "original_assignee_id", "delegation_id", "status", "activated_at",
                      "due_at", "decided_at", "decision_comment"):
                setattr(o, f, getattr(st, f))
            st.dirty = False

    # ------------------------------------------------------------------ queries

    async def get_route(self, caller: Caller, route_id: uuid.UUID) -> views.RouteView:
        async with self._tx() as s:
            user = await self._user(s, caller)
            route = await s.get(ApprovalRoute, route_id)
            if route is None:
                raise NotFound("Маршрут не знайдено")
            author = await s.scalar(select(Document.author_id).where(Document.id == route.document_id))
            participant = await s.scalar(select(exists().where(
                ApprovalStep.route_id == route_id,
                or_(ApprovalStep.assignee_id == user.id, ApprovalStep.original_assignee_id == user.id))))
            if user.id not in (route.initiated_by_id, author) and not participant:
                raise AccessDenied("Маршрут доступний ініціатору, автору й учасникам погодження")
            return await self._view(s, route_id)

    async def current_route_id(self, caller: Caller, document_id: uuid.UUID) -> uuid.UUID:
        async with self._tx() as s:
            await self._user(s, caller)
            rid = await s.scalar(select(ApprovalRoute.id).where(ApprovalRoute.document_id == document_id)
                                 .order_by(ApprovalRoute.created_at.desc()).limit(1))
            if rid is None:
                raise NotFound("Для документа ще не створено маршрут")
            return rid

    async def inbox(self, caller: Caller) -> list[views.InboxItem]:
        async with self._tx() as s:
            user = await self._user(s, caller)
            now = self.clock()
            rows = (await s.execute(
                select(ApprovalStep, ApprovalRoute, Document.title, DocumentType.code)
                .join(ApprovalRoute, ApprovalRoute.id == ApprovalStep.route_id)
                .join(Document, Document.id == ApprovalRoute.document_id)
                .join(DocumentType, DocumentType.id == Document.document_type_id)
                .where(ApprovalStep.assignee_id == user.id, ApprovalStep.status == StepStatus.ACTIVE,
                       ApprovalRoute.status == RouteStatus.ACTIVE)
                .order_by(ApprovalStep.due_at.nulls_last(), ApprovalStep.activated_at)
            )).all()
            out = []
            tpl_cache: dict[uuid.UUID | None, TemplateRef | None] = {}
            for step, route, title, type_code in rows:
                if route.template_id not in tpl_cache:
                    tpl_cache[route.template_id] = await self._template_by_id(s, route.template_id)
                out.append(views.InboxItem(
                    step_id=step.id, route_id=route.id, document_id=route.document_id, document_title=title,
                    document_type_code=type_code, step_kind=step.step_kind,
                    title_uk=self._title(tpl_cache[route.template_id], step), requires_kep=step.requires_kep,
                    activated_at=step.activated_at, due_at=step.due_at,
                    is_overdue=bool(step.due_at and step.due_at < now),
                    is_delegated=step.original_assignee_id is not None))
            return out

    @staticmethod
    def _title(tpl: TemplateRef | None, step: ApprovalStep) -> str:
        for r in tpl.rules if tpl else ():
            if r.template_step_id == step.template_step_id:
                return r.extras.title_uk
        return LABELS_UK.get(step.acting_role) if step.acting_role else LABELS_UK[step.step_kind]  # type: ignore[return-value]

    async def _view(self, s: AsyncSession, route_id: uuid.UUID, warnings: list[str] | None = None) -> views.RouteView:
        route = await s.get(ApprovalRoute, route_id, populate_existing=True)
        doc = await s.get(Document, route.document_id)
        ver = await s.get(DocumentVersion, route.document_version_id)
        steps = list(await s.scalars(select(ApprovalStep).where(ApprovalStep.route_id == route_id)
                                     .order_by(ApprovalStep.stage_no, ApprovalStep.ordinal)
                                     .execution_options(populate_existing=True)))
        user_ids = {x.assignee_id for x in steps} | {x.original_assignee_id for x in steps if x.original_assignee_id}
        names = {u.id: f"{u.first_name} {u.last_name.upper()}"
                 for u in await s.scalars(select(User).where(User.id.in_(user_ids)))}
        sigs = {g.approval_step_id: g for g in await s.scalars(
            select(Signature).where(Signature.approval_step_id.in_([x.id for x in steps])))}
        tpl = await self._template_by_id(s, route.template_id)
        now = self.clock()
        return views.RouteView(
            id=route.id, document_id=doc.id, document_title=doc.title, document_version_id=ver.id,
            content_sha256=ver.content_sha256, template_code=tpl.code if tpl else None,
            template_version=tpl.version if tpl else None, template_name=tpl.name_uk if tpl else None,
            status=route.status, current_stage=route.current_stage, initiated_by_id=route.initiated_by_id,
            started_at=route.started_at, completed_at=route.completed_at, cancel_reason=route.cancel_reason,
            warnings=warnings or [],
            steps=[views.StepView(
                id=x.id, stage_no=x.stage_no, ordinal=x.ordinal, title_uk=self._title(tpl, x),
                step_kind=x.step_kind, assignee_id=x.assignee_id, assignee_name=names.get(x.assignee_id, "?"),
                original_assignee_id=x.original_assignee_id,
                original_assignee_name=names.get(x.original_assignee_id) if x.original_assignee_id else None,
                acting_role=x.acting_role, status=x.status, is_mandatory=x.is_mandatory,
                requires_kep=x.requires_kep, activated_at=x.activated_at, due_at=x.due_at,
                decided_at=x.decided_at, decision_comment=x.decision_comment,
                is_overdue=bool(x.status is StepStatus.ACTIVE and x.due_at and x.due_at < now),
                signature=None if (g := sigs.get(x.id)) is None else views.SignatureView(
                    id=g.id, cert_subject_cn=g.cert_subject_cn, cert_serial=g.cert_serial,
                    cert_issuer=g.cert_issuer, signed_at=g.signed_at, validation_status=g.validation_status))
                for x in steps])
