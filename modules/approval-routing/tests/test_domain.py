"""Чиста логіка: машина станів маршруту, визначення учасників, каталог шаблонів (без БД)."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from sed_db.enums import AssigneeRule, DocumentStatus, OrgUnitKind, RouteStatus, StepKind, StepStatus, UserRole
from sed_routing.domain import state_machine as sm
from sed_routing.domain.errors import AccessDenied, AssigneeUnresolved, InvalidState, TemplateConfigError
from sed_routing.domain.models import DelegationInfo, DocumentContext, Holder, StepRule, UnitInfo
from sed_routing.domain.resolution import AssigneeResolver
from sed_routing.domain.templates import StepExtras, TemplateCatalog, TemplateSpec

NOW = datetime(2026, 10, 17, 10, 0, tzinfo=timezone.utc)
A, B, C, D = (uuid.uuid4() for _ in range(4))


def route(*steps: tuple[int, uuid.UUID, bool]) -> sm.RouteState:
    ordinals: dict[int, int] = {}
    out = []
    for stage, who, mandatory in steps:
        ordinals[stage] = ordinals.get(stage, 0) + 1
        out.append(sm.StepState(id=uuid.uuid4(), stage_no=stage, ordinal=ordinals[stage], step_kind=StepKind.VISA,
                                assignee_id=who, is_mandatory=mandatory))
    return sm.RouteState(id=uuid.uuid4(), document_id=uuid.uuid4(), document_version_id=uuid.uuid4(),
                         status=RouteStatus.DRAFT, initiated_by_id=uuid.uuid4(), steps=out)


def run(r: sm.RouteState) -> None:
    """Спрощена версія ApprovalService._advance без БД."""
    while True:
        nxt, stage = sm.evaluate(r)
        if nxt is sm.Next.WAIT:
            return
        if nxt is sm.Next.REJECT:
            sm.close_open_steps(r, stage, NOW, "відхилено")
            sm.finish(r, RouteStatus.REJECTED, NOW)
            return
        if r.current_stage is not None:
            sm.close_open_steps(r, r.current_stage, NOW, "етап завершено")
        if nxt is sm.Next.COMPLETE:
            sm.finish(r, RouteStatus.COMPLETED, NOW)
            return
        r.current_stage = stage
        for s in r.stage_steps(stage):
            s.set(status=StepStatus.ACTIVE, activated_at=NOW)


def approve(r: sm.RouteState, idx: int, who: uuid.UUID, decision: StepStatus = StepStatus.APPROVED,
            comment: str | None = None) -> None:
    s = r.steps[idx]
    sm.ensure_can_decide(r, s, who)
    sm.record_decision(s, decision, comment, NOW)
    run(r)


def test_strictly_sequential() -> None:
    r = route((1, A, True), (2, B, True), (3, C, True))
    sm.start(r, NOW)
    run(r)
    assert [s.status for s in r.steps] == [StepStatus.ACTIVE, StepStatus.WAITING, StepStatus.WAITING]
    with pytest.raises(InvalidState, match="Етап ще не настав"):
        approve(r, 1, B)
    with pytest.raises(AccessDenied):
        approve(r, 0, B)
    approve(r, 0, A)
    assert r.current_stage == 2 and r.steps[1].status is StepStatus.ACTIVE
    with pytest.raises(InvalidState, match="вже прийнято"):
        approve(r, 0, A)
    approve(r, 1, B, StepStatus.APPROVED_WITH_REMARKS, "Уточнити кошторис")
    approve(r, 2, C)
    assert r.status is RouteStatus.COMPLETED


def test_remarks_and_rejection_need_comment() -> None:
    r = route((1, A, True))
    sm.start(r, NOW)
    run(r)
    with pytest.raises(InvalidState, match="обов'язкові"):
        approve(r, 0, A, StepStatus.REJECTED, "  ")
    approve(r, 0, A, StepStatus.REJECTED, "Немає обґрунтування")
    assert r.status is RouteStatus.REJECTED


def test_parallel_stage_waits_for_all_mandatory() -> None:
    r = route((1, A, True), (1, B, True), (1, D, False), (2, C, True))
    sm.start(r, NOW)
    run(r)
    assert [s.status for s in r.steps[:3]] == [StepStatus.ACTIVE] * 3
    approve(r, 0, A)
    assert r.current_stage == 1, "другий обов'язковий учасник етапу ще не завізував"
    approve(r, 1, B)
    assert r.current_stage == 2
    assert r.steps[2].status is StepStatus.SKIPPED, "необов'язковий крок закривається разом з етапом"


def test_optional_rejection_does_not_stop_route() -> None:
    r = route((1, A, True), (1, D, False), (2, C, True))
    sm.start(r, NOW)
    run(r)
    approve(r, 1, D, StepStatus.REJECTED, "Заперечую, але не блокую")
    assert r.status is RouteStatus.ACTIVE and r.current_stage == 1
    approve(r, 0, A)
    assert r.current_stage == 2


def test_mandatory_rejection_closes_parallel_steps() -> None:
    r = route((1, A, True), (1, B, True), (2, C, True))
    sm.start(r, NOW)
    run(r)
    approve(r, 0, A, StepStatus.REJECTED, "Ні")
    assert r.status is RouteStatus.REJECTED
    assert r.steps[1].status is StepStatus.SKIPPED and r.steps[2].status is StepStatus.WAITING


def test_cancel_and_restart_rules() -> None:
    r = route((1, A, True))
    with pytest.raises(InvalidState):
        sm.ensure_can_decide(r, r.steps[0], A)
    sm.start(r, NOW)
    with pytest.raises(InvalidState):
        sm.start(r, NOW)
    run(r)
    sm.finish(r, RouteStatus.CANCELLED, NOW, "Скасовано автором")
    assert r.steps[0].status is StepStatus.SKIPPED and r.cancel_reason == "Скасовано автором"


# --------------------------------------------------------------------------- resolution

UNI, INST, DEPT, BUH = (uuid.uuid4() for _ in range(4))


class FakeDirectory:
    def __init__(self) -> None:
        self.units = {
            UNI: UnitInfo(UNI, OrgUnitKind.UNIVERSITY, "PUET", "ПУЕТ", None),
            INST: UnitInfo(INST, OrgUnitKind.INSTITUTE, "NNI", "ННІ", UNI),
            DEPT: UnitInfo(DEPT, OrgUnitKind.DEPARTMENT, "K", "Кафедра", INST),
            BUH: UnitInfo(BUH, OrgUnitKind.DIVISION, "BUH", "Бухгалтерія", UNI),
        }
        self.roles: list[Holder] = []
        self.delegations: dict[uuid.UUID, DelegationInfo] = {}
        self.inactive: set[uuid.UUID] = set()

    async def unit_chain(self, unit_id):  # noqa: ANN001, ANN201
        out, cur = [], unit_id
        while cur:
            out.append(self.units[cur])
            cur = self.units[cur].parent_id
        return out

    async def role_holders(self, role, unit_id, on, *, any_unit=False):  # noqa: ANN001, ANN201
        return [h for h in self.roles if h.role is role and (any_unit or h.org_unit_id == unit_id)]

    async def position_holders(self, unit_id, position_code, on):  # noqa: ANN001, ANN201
        return []

    async def user(self, user_id):  # noqa: ANN001, ANN201
        return None if user_id in self.inactive else Holder(user_id, "X")

    async def active_delegation(self, user_id, document_type_id, at):  # noqa: ANN001, ANN201
        return self.delegations.get(user_id)


def rule(r: AssigneeRule, title: str = "Посада", **kw) -> StepRule:  # noqa: ANN003
    return StepRule(stage_no=1, ordinal=1, step_kind=StepKind.VISA, rule=r,
                    extras=StepExtras(title_uk=title, role_scope=kw.pop("scope", None),
                                      skip_if_author=kw.pop("skip", False)), **kw)


DOC = DocumentContext(id=uuid.uuid4(), document_type_id=uuid.uuid4(), document_type_code="MEMO", org_unit_id=DEPT,
                      author_id=A, status=DocumentStatus.DRAFT, current_version_id=uuid.uuid4(),
                      current_version_sha256="0" * 64, title="t")


@pytest.fixture()
def directory() -> FakeDirectory:
    d = FakeDirectory()
    d.roles = [
        Holder(B, "Андрій КОВАЛЬ", UserRole.DEPARTMENT_HEAD, DEPT),
        Holder(C, "Директор", UserRole.INSTITUTE_DIRECTOR, INST),
        Holder(D, "Петро ГОСПОДАРЕНКО", UserRole.VICE_RECTOR, None, scope_note="З адміністративно-господарської  роботи"),
        Holder(uuid.uuid4(), "Ольга НАУМЕНКО", UserRole.VICE_RECTOR, None, scope_note="з науково-педагогічної роботи"),
    ]
    return d


async def test_author_unit_head_and_unit_role(directory: FakeDirectory) -> None:
    res = AssigneeResolver(directory)
    p = await res.plan_step(rule(AssigneeRule.AUTHOR_UNIT_HEAD), DOC, date.today())
    assert p.assignee.user_id == B and p.acting_role is UserRole.DEPARTMENT_HEAD
    # Директор «свого» інституту: шукаємо вгору по ієрархії від кафедри автора.
    p = await res.plan_step(rule(AssigneeRule.AUTHOR_UNIT_ROLE, role=UserRole.INSTITUTE_DIRECTOR), DOC, date.today())
    assert p.assignee.user_id == C


async def test_vice_rector_by_scope_normalised(directory: FakeDirectory) -> None:
    res = AssigneeResolver(directory)
    p = await res.plan_step(rule(AssigneeRule.ROLE, role=UserRole.VICE_RECTOR,
                                 scope="з адміністративно-господарської роботи"), DOC, date.today())
    assert p.assignee.user_id == D
    with pytest.raises(AssigneeUnresolved, match="кількох"):
        await res.plan_step(rule(AssigneeRule.ROLE, role=UserRole.VICE_RECTOR), DOC, date.today())


async def test_vacancy_and_skip_if_author(directory: FakeDirectory) -> None:
    res = AssigneeResolver(directory)
    with pytest.raises(AssigneeUnresolved, match="вакантна"):
        await res.plan_step(rule(AssigneeRule.UNIT_HEAD, "Головний бухгалтер", unit_id=BUH), DOC, date.today())
    doc_by_head = DocumentContext(**{**DOC.__dict__, "author_id": B})
    p = await res.plan_step(rule(AssigneeRule.AUTHOR_UNIT_HEAD, skip=True), doc_by_head, date.today())
    assert p.will_skip and "Автор" in p.skip_reason


async def test_delegation_chain_and_cycle(directory: FakeDirectory) -> None:
    res = AssigneeResolver(directory)
    until = NOW + timedelta(days=3)
    directory.delegations = {B: DelegationInfo(uuid.uuid4(), B, C, until),
                             C: DelegationInfo(uuid.uuid4(), C, D, until)}
    final, d = await res.effective_assignee(B, DOC.document_type_id, NOW)
    assert final == D and d.from_user_id == C, "ланцюжок заміщень"
    directory.delegations[D] = DelegationInfo(uuid.uuid4(), D, B, until)
    final, _ = await res.effective_assignee(B, DOC.document_type_id, NOW)
    assert final == D, "цикл заміщень не зациклює і не повертає до відсутнього"
    directory.inactive.add(C)
    directory.delegations = {B: DelegationInfo(uuid.uuid4(), B, C, until)}
    final, d = await res.effective_assignee(B, DOC.document_type_id, NOW)
    assert final == B and d is None, "звільнений заступник — візує сам власник посади"


# --------------------------------------------------------------------------- templates

def test_procurement_template_is_sequential(catalog: TemplateCatalog) -> None:
    spec = catalog.get("MEMO_PROCUREMENT")
    assert [(st, o) for st, o, _ in spec.numbered_steps()] == [(1, 1), (2, 1), (3, 1), (4, 1), (5, 1)]
    assert all(s.requires_kep for s in spec.steps)


def test_template_validation() -> None:
    base = {"code": "X", "name_uk": "X", "document_type": "MEMO"}
    with pytest.raises(ValueError, match="unit_head потребує unit"):
        TemplateSpec.model_validate({**base, "steps": [{"title_uk": "a", "kind": "visa", "rule": "unit_head"}]})
    with pytest.raises(ValueError, match="role_scope"):
        TemplateSpec.model_validate({**base, "steps": [{"title_uk": "a", "kind": "visa", "rule": "author_unit_head",
                                                        "role_scope": "x"}]})
    bad = TemplateSpec.model_validate({**base, "steps": [
        {"title_uk": "a", "kind": "visa", "rule": "author_unit_head", "stage": 2},
        {"title_uk": "b", "kind": "visa", "rule": "author_unit_head", "stage": 1}]})
    with pytest.raises(TemplateConfigError, match="зростанням"):
        TemplateCatalog([bad])
