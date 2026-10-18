"""Наскрізні сценарії на живому PostgreSQL: службова записка на закупівлю від кафедри
проходить зав. кафедри → проректора → головного бухгалтера → ректора → ПФВ, кожен з КЕП."""

from __future__ import annotations

import base64
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, text

from sed_db.enums import (
    DocumentStatus,
    FindingCategory,
    FindingSeverity,
    RouteStatus,
    SignatureValidation,
    StepStatus,
    UserRole,
)
from sed_db.models import (
    ApprovalStepHistory,
    AuditLog,
    Delegation,
    Document,
    DocumentStatusHistory,
    DocumentVersion,
    RouteTemplate,
    Signature,
    UserRoleAssignment,
)
from sed_db.schemas import StepDecision
from sed_routing import views
from sed_routing.domain.errors import (
    AccessDenied,
    AssigneeUnresolved,
    BlockingFindings,
    InvalidState,
    SignatureRejected,
)

from .conftest import as_system, caller, make_memo, now

CHAIN = ["head", "vr_ahr", "chief_acc", "rector", "pfv_head"]


def key_of(dept, k: str) -> str:  # noqa: ANN001
    return dept.head if k == "head" else k


def sign_req(world, key: str, sha256: str, **kw) -> views.SignRequest:  # noqa: ANN001, ANN003
    container = world.keys[key].sign(sha256, kw.pop("signing_time", None))
    return views.SignRequest(signature_base64=base64.b64encode(container).decode(), **kw)


async def test_preview_lists_officials_in_order(service, world, dept, sessionmaker) -> None:  # noqa: ANN001
    doc_id, _ = await make_memo(sessionmaker, world, dept.id, dept.author)
    plan = await service.preview(caller(world, dept.author), doc_id, views.RouteRequest())
    assert plan.template_code == "MEMO_PROCUREMENT"
    assert [s.title_uk for s in plan.steps] == [
        "Завідувач кафедри", "Проректор (за напрямом)", "Головний бухгалтер", "Ректор", "Планово-фінансовий відділ"]
    assert [s.assignee_id for s in plan.steps] == [world.ids[key_of(dept, k)] for k in CHAIN]
    assert [s.stage_no for s in plan.steps] == [1, 2, 3, 4, 5], "строго послідовно"
    assert all(s.requires_kep for s in plan.steps)
    assert plan.steps[0].assignee_name == "Андрій КОВАЛЬ"


async def test_full_procurement_chain_with_kep(service, world, dept, sessionmaker, store) -> None:  # noqa: ANN001
    doc_id, sha256 = await make_memo(sessionmaker, world, dept.id, dept.author)
    route = await service.create_route(caller(world, dept.author), doc_id, views.RouteCreate())
    assert route.status is RouteStatus.ACTIVE and route.current_stage == 1
    assert [s.status for s in route.steps] == [StepStatus.ACTIVE] + [StepStatus.WAITING] * 4
    assert route.steps[0].due_at is not None

    # Наступний учасник не може підписати, поки попередній не завізував.
    with pytest.raises(InvalidState, match="Етап ще не настав"):
        await service.sign(caller(world, "vr_ahr"), route.steps[1].id, sign_req(world, "vr_ahr", sha256))

    # Вхідні: документ бачить лише той, чия черга.
    assert [i.step_id for i in await service.inbox(caller(world, dept.head))] == [route.steps[0].id]
    assert [i for i in await service.inbox(caller(world, "vr_ahr")) if i.route_id == route.id] == []

    for i, k in enumerate(CHAIN):
        who = key_of(dept, k)
        req = await service.signing_request(caller(world, who), route.steps[i].id)
        assert req.content_sha256 == sha256
        route = await service.sign(caller(world, who), route.steps[i].id, sign_req(world, who, req.content_sha256))
        assert route.steps[i].status is StepStatus.APPROVED
        assert route.steps[i].signature.validation_status is SignatureValidation.VALID
        if i + 1 < len(CHAIN):
            assert route.current_stage == i + 2
            assert route.steps[i + 1].status is StepStatus.ACTIVE
            assert all(s.status is StepStatus.WAITING for s in route.steps[i + 2:])

    assert route.status is RouteStatus.COMPLETED and route.completed_at is not None
    assert len(store.objects) == 5

    async with sessionmaker() as s:
        doc = await s.get(Document, doc_id)
        assert doc.status is DocumentStatus.APPROVED
        sigs = list(await s.scalars(select(Signature).join(DocumentVersion).where(DocumentVersion.document_id == doc_id)))
        assert len(sigs) == 5 and {g.signed_content_sha256 for g in sigs} == {sha256}
        assert all(g.signer_tax_id_hmac and g.storage_key in store.objects for g in sigs)
        statuses = list(await s.scalars(select(DocumentStatusHistory.to_status)
                                        .where(DocumentStatusHistory.document_id == doc_id)
                                        .order_by(DocumentStatusHistory.id)))
        assert statuses == [DocumentStatus.DRAFT, DocumentStatus.ON_APPROVAL, DocumentStatus.APPROVED]
        hist = (await s.execute(select(ApprovalStepHistory.to_status, ApprovalStepHistory.changed_by_id,
                                       ApprovalStepHistory.actor_type)
                                .where(ApprovalStepHistory.step_id == route.steps[2].id)
                                .order_by(ApprovalStepHistory.id))).all()
        # Активує система (після візи проректора), погоджує сам головний бухгалтер.
        assert [(h[0], h[1]) for h in hist] == [
            (StepStatus.WAITING, world.ids[dept.author]), (StepStatus.ACTIVE, None),
            (StepStatus.APPROVED, world.ids["chief_acc"])]
        ver = await s.get(DocumentVersion, route.document_version_id)
        assert ver.is_locked
        assert (await s.execute(text("SELECT * FROM sed_audit_verify()"))).all() == []


async def test_invalid_signature_does_not_advance(service, world, dept, sessionmaker, store) -> None:  # noqa: ANN001
    doc_id, sha256 = await make_memo(sessionmaker, world, dept.id, dept.author)
    route = await service.create_route(caller(world, dept.author), doc_id, views.RouteCreate())
    step = route.steps[0]

    other_doc = "a" * 64
    with pytest.raises(SignatureRejected, match="Геш у підписі не збігається"):
        await service.sign(caller(world, dept.head), step.id, sign_req(world, dept.head, other_doc))
    # Чужий ключ (ключ ректора) на кроці завідувача.
    with pytest.raises(SignatureRejected, match="належить іншій особі"):
        await service.sign(caller(world, dept.head), step.id, sign_req(world, "rector", sha256))
    # Контейнер, підготовлений заздалегідь — до того, як документ надійшов на візу.
    with pytest.raises(SignatureRejected, match="раніше, ніж документ надійшов"):
        await service.sign(caller(world, dept.head), step.id,
                           sign_req(world, dept.head, sha256, signing_time=now() - timedelta(hours=2)))

    route = await service.get_route(caller(world, dept.author), route.id)
    assert route.current_stage == 1 and route.steps[0].status is StepStatus.ACTIVE
    assert route.steps[1].status is StepStatus.WAITING
    assert store.objects == {}, "відхилений підпис не зберігається"
    async with sessionmaker() as s:
        rejected = list(await s.scalars(select(AuditLog.action).where(
            AuditLog.document_id == doc_id, AuditLog.action == "kep.signature_rejected")))
        assert len(rejected) == 3

    # Підписати чужий крок неможливо навіть валідним ключем.
    with pytest.raises(AccessDenied):
        await service.sign(caller(world, "rector"), step.id, sign_req(world, "rector", sha256))


async def test_reject_returns_document_for_revision(service, world, dept, sessionmaker) -> None:  # noqa: ANN001
    doc_id, sha256 = await make_memo(sessionmaker, world, dept.id, dept.author)
    route = await service.create_route(caller(world, dept.author), doc_id, views.RouteCreate())
    route = await service.sign(caller(world, dept.head), route.steps[0].id, sign_req(world, dept.head, sha256))

    with pytest.raises(InvalidState, match="лише з накладанням КЕП"):
        await service.decide(caller(world, "vr_ahr"), route.steps[1].id, StepDecision(decision=StepStatus.APPROVED))

    route = await service.decide(caller(world, "vr_ahr"), route.steps[1].id,
                                 StepDecision(decision=StepStatus.REJECTED, comment="Немає комерційних пропозицій"))
    assert route.status is RouteStatus.REJECTED
    assert route.steps[1].decision_comment == "Немає комерційних пропозицій"
    assert all(s.status is StepStatus.WAITING for s in route.steps[2:])
    async with sessionmaker() as s:
        assert (await s.get(Document, doc_id)).status is DocumentStatus.REVISION_REQUIRED

    with pytest.raises(InvalidState, match="не активний"):
        await service.sign(caller(world, "chief_acc"), route.steps[2].id, sign_req(world, "chief_acc", sha256))


async def test_head_as_author_skips_own_visa(service, world, dept, sessionmaker) -> None:  # noqa: ANN001
    doc_id, _ = await make_memo(sessionmaker, world, dept.id, dept.head)
    plan = await service.preview(caller(world, dept.head), doc_id, views.RouteRequest())
    assert plan.steps[0].will_skip
    route = await service.create_route(caller(world, dept.head), doc_id, views.RouteCreate())
    assert route.steps[0].status is StepStatus.SKIPPED
    assert route.current_stage == 2 and route.steps[1].status is StepStatus.ACTIVE


async def test_delegation_goes_to_deputy(service, world, dept, sessionmaker, knedp, binding, hasher) -> None:  # noqa: ANN001
    from .conftest import add_user

    async with sessionmaker() as s, s.begin():
        await as_system(s)
        deputy = await add_user(s, world, f"deputy-{dept.id.hex[:6]}", "Заступенко", "Ірина", dept.id,
                                knedp=knedp, binding=binding, hasher=hasher)
        s.add(Delegation(from_user_id=world.ids[dept.head], to_user_id=deputy, valid_from=now() - timedelta(days=1),
                         valid_to=now() + timedelta(days=7), basis="Наказ про відпустку № 12-в",
                         created_by_id=world.ids[dept.head]))
    doc_id, sha256 = await make_memo(sessionmaker, world, dept.id, dept.author)
    route = await service.create_route(caller(world, dept.author), doc_id, views.RouteCreate())
    step = route.steps[0]
    assert step.assignee_id == deputy and step.original_assignee_id == world.ids[dept.head]
    with pytest.raises(AccessDenied):
        await service.sign(caller(world, dept.head), step.id, sign_req(world, dept.head, sha256))
    route = await service.sign(caller(world, f"deputy-{dept.id.hex[:6]}"), step.id,
                               sign_req(world, f"deputy-{dept.id.hex[:6]}", sha256))
    assert route.steps[0].status is StepStatus.APPROVED and route.current_stage == 2


async def test_vacancy_and_ambiguous_vice_rector(service, world, dept, sessionmaker) -> None:  # noqa: ANN001
    from sed_routing.domain.templates import TemplateCatalog, TemplateSpec

    doc_id, _ = await make_memo(sessionmaker, world, dept.id, dept.author)
    spec = service.catalog.get("MEMO_PROCUREMENT")
    no_scope = spec.model_copy(update={"steps": tuple(
        st.model_copy(update={"role_scope": None}) if st.role is UserRole.VICE_RECTOR else st for st in spec.steps)})
    original = service.catalog
    service.catalog = TemplateCatalog([TemplateSpec.model_validate(no_scope.model_dump())])
    try:
        with pytest.raises(AssigneeUnresolved, match="кількох осіб") as exc:
            await service.preview(caller(world, dept.author), doc_id, views.RouteRequest())
        assert {c["user_id"] for c in exc.value.details["candidates"]} == {
            str(world.ids["vr_ahr"]), str(world.ids["vr_npr"])}
        plan = await service.preview(caller(world, dept.author), doc_id, views.RouteRequest(
            assignee_overrides=[views.AssigneeOverride(stage_no=2, ordinal=1, user_id=world.ids["vr_npr"])]))
        assert plan.steps[1].assignee_id == world.ids["vr_npr"]
    finally:
        service.catalog = original

    # Обрати можна лише проректора, а не довільну людину.
    with pytest.raises(AssigneeUnresolved, match="не обіймає посаду"):
        await service.preview(caller(world, dept.author), doc_id, views.RouteRequest(
            assignee_overrides=[views.AssigneeOverride(stage_no=2, ordinal=1, user_id=world.ids["outsider"])]))

    # Завідувача звільнили — посада вакантна, маршрут не створюється «в нікуди».
    async with sessionmaker() as s, s.begin():
        await as_system(s)
        ra = await s.scalar(select(UserRoleAssignment).where(UserRoleAssignment.user_id == world.ids[dept.head]))
        ra.valid_to = now().date()
    with pytest.raises(AssigneeUnresolved, match="вакантна"):
        await service.create_route(caller(world, dept.author), doc_id, views.RouteCreate())


async def test_critical_findings_block_start(service, world, dept, sessionmaker) -> None:  # noqa: ANN001
    from decimal import Decimal

    from sed_db.enums import AiRunStatus, AiTaskType, ComplianceVerdict, ModelProvider
    from sed_db.models import AiModel, AiRun, ComplianceCheck, ComplianceFinding

    doc_id, _ = await make_memo(sessionmaker, world, dept.id, dept.author)
    async with sessionmaker() as s, s.begin():
        await as_system(s)
        doc = await s.get(Document, doc_id)
        model = AiModel(name=f"rag-{uuid.uuid4().hex[:6]}", version="1", provider=ModelProvider.VLLM,
                        task_type=AiTaskType.COMPLIANCE_CHECK)
        s.add(model)
        await s.flush()
        run = AiRun(task_type=AiTaskType.COMPLIANCE_CHECK, status=AiRunStatus.SUCCEEDED, document_id=doc_id,
                    document_version_id=doc.current_version_id, model_id=model.id, pii_masked=True,
                    confidence=Decimal("0.9"), started_at=now(), finished_at=now())
        s.add(run)
        await s.flush()
        check = ComplianceCheck(ai_run_id=run.id, document_version_id=doc.current_version_id,
                                verdict=ComplianceVerdict.NON_COMPLIANT, regulations_as_of=now().date())
        s.add(check)
        await s.flush()
        s.add(ComplianceFinding(check_id=check.id, ordinal=1, severity=FindingSeverity.CRITICAL,
                                category=FindingCategory.COMPETENCE_VIOLATION, title="Перевищення повноважень",
                                explanation="Закупівлю понад ліміт погоджує тендерний комітет",
                                confidence=Decimal("0.9")))
    with pytest.raises(BlockingFindings):
        await service.create_route(caller(world, dept.author), doc_id, views.RouteCreate())
    route = await service.create_route(caller(world, dept.author), doc_id,
                                       views.RouteCreate(acknowledge_findings=True))
    assert route.status is RouteStatus.ACTIVE
    async with sessionmaker() as s:
        assert await s.scalar(select(AuditLog.id).where(AuditLog.document_id == doc_id,
                                                        AuditLog.action == "route.findings_acknowledged"))


async def test_access_rules(service, world, dept, sessionmaker) -> None:  # noqa: ANN001
    doc_id, sha256 = await make_memo(sessionmaker, world, dept.id, dept.author)
    with pytest.raises(AccessDenied):
        await service.create_route(caller(world, "outsider"), doc_id, views.RouteCreate())
    with pytest.raises(AccessDenied, match="ШІ"):
        await service.create_route(caller(world, dept.author, "sed-ai-service"), doc_id, views.RouteCreate())
    route = await service.create_route(caller(world, dept.author), doc_id, views.RouteCreate())
    with pytest.raises(InvalidState, match="не можна відправити на погодження"):
        await service.create_route(caller(world, dept.author), doc_id, views.RouteCreate())
    with pytest.raises(AccessDenied):
        await service.get_route(caller(world, "outsider"), route.id)
    with pytest.raises(AccessDenied):
        await service.sign(caller(world, dept.head, "sed-ai-service"), route.steps[0].id,
                           sign_req(world, dept.head, sha256))

    route = await service.cancel_route(caller(world, dept.author), route.id, views.RouteCancel(reason="Помилка в сумі"))
    assert route.status is RouteStatus.CANCELLED and route.steps[0].status is StepStatus.SKIPPED
    async with sessionmaker() as s:
        assert (await s.get(Document, doc_id)).status is DocumentStatus.DRAFT


async def test_new_version_during_route_blocks_decisions(service, world, dept, sessionmaker) -> None:  # noqa: ANN001
    from sed_db.enums import VersionSource

    doc_id, sha256 = await make_memo(sessionmaker, world, dept.id, dept.author)
    route = await service.create_route(caller(world, dept.author), doc_id, views.RouteCreate())
    async with sessionmaker() as s, s.begin():
        await as_system(s)
        doc = await s.get(Document, doc_id)
        v2 = DocumentVersion(document_id=doc_id, version_number=2, source=VersionSource.EDITOR,
                             storage_bucket="sed-documents", storage_key=f"{doc_id}/v2.pdf", file_name="v2.pdf",
                             mime_type="application/pdf", size_bytes=3, content_sha256="b" * 64,
                             created_by_id=world.ids[dept.author])
        s.add(v2)
        await s.flush()
        doc.current_version_id = v2.id
    with pytest.raises(InvalidState, match="новішу версію"):
        await service.sign(caller(world, dept.head), route.steps[0].id, sign_req(world, dept.head, sha256))


async def test_template_sync_is_versioned(sessionmaker, catalog) -> None:  # noqa: ANN001
    from sed_db.seeds import SYSTEM_USER_ID
    from sed_routing.adapters.template_sync import sync_templates
    from sed_routing.domain.templates import TemplateCatalog

    async with sessionmaker() as s:
        assert [r.action for r in await sync_templates(s, catalog, SYSTEM_USER_ID)] == ["unchanged"]
        spec = catalog.get("MEMO_PROCUREMENT")
        changed = spec.model_copy(update={"steps": spec.steps[:-1]})
        r = await sync_templates(s, TemplateCatalog([changed]), SYSTEM_USER_ID)
        assert [(x.action, x.version) for x in r] == [("updated", 2)]
        active = list(await s.scalars(select(RouteTemplate.version).where(
            RouteTemplate.code == "MEMO_PROCUREMENT", RouteTemplate.is_active)))
        assert active == [2]
        await s.rollback()

