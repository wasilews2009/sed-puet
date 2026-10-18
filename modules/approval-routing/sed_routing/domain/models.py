"""Об'єкти предметної області, якими обмінюються сервіс, резолвер і адаптери."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sed_db.enums import AssigneeRule, DocumentStatus, OrgUnitKind, StepKind, UserRole

from .templates import StepExtras


@dataclass(frozen=True)
class Holder:
    """Людина, що обіймає посаду/роль, яка має візувати."""

    user_id: uuid.UUID
    display_name: str               # «Ім'я ПРІЗВИЩЕ» (ДСТУ 4163)
    role: UserRole | None = None
    org_unit_id: uuid.UUID | None = None
    is_acting: bool = False         # т.в.о.
    scope_note: str | None = None   # напрям проректора


@dataclass(frozen=True)
class UnitInfo:
    id: uuid.UUID
    kind: OrgUnitKind
    code: str
    name_uk: str
    parent_id: uuid.UUID | None


@dataclass(frozen=True)
class DelegationInfo:
    id: uuid.UUID
    from_user_id: uuid.UUID
    to_user_id: uuid.UUID
    valid_to: datetime


@dataclass(frozen=True)
class DocumentContext:
    id: uuid.UUID
    document_type_id: uuid.UUID
    document_type_code: str
    org_unit_id: uuid.UUID
    author_id: uuid.UUID
    status: DocumentStatus
    current_version_id: uuid.UUID | None
    current_version_sha256: str | None
    title: str


@dataclass(frozen=True)
class StepRule:
    """Крок шаблону (з `route_template_steps`) + додаткові поля з каталогу."""

    stage_no: int
    ordinal: int
    step_kind: StepKind
    rule: AssigneeRule
    extras: StepExtras
    template_step_id: uuid.UUID | None = None
    role: UserRole | None = None
    unit_id: uuid.UUID | None = None
    position_code: str | None = None
    user_id: uuid.UUID | None = None
    deadline_hours: int | None = None
    is_mandatory: bool = True
    requires_kep: bool = False


@dataclass(frozen=True)
class TemplateRef:
    id: uuid.UUID
    code: str
    version: int
    name_uk: str
    org_unit_id: uuid.UUID | None
    rules: tuple[StepRule, ...]


@dataclass(frozen=True)
class PlannedStep:
    """Результат визначення учасника для кроку (до запуску маршруту)."""

    rule: StepRule
    assignee: Holder
    acting_role: UserRole | None
    will_skip: bool = False           # автор сам обіймає посаду
    skip_reason: str | None = None


@dataclass(frozen=True)
class RoutePlan:
    document: DocumentContext
    template: TemplateRef
    steps: tuple[PlannedStep, ...]
    warnings: tuple[str, ...] = field(default=())
