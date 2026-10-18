"""Маршрути візування, рішення по кроках та історія погодження."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from ..enums import (
    AssigneeRule,
    AuditActorType,
    RouteOrigin,
    RouteStatus,
    StepKind,
    StepStatus,
    UserRole,
)
from .common import InputModel, NonEmptyStr, ReadModel


class RouteTemplateStepIn(InputModel):
    stage_no: int = Field(ge=1, description="Кроки з однаковим stage_no виконуються паралельно")
    ordinal: int = Field(ge=1)
    step_kind: StepKind
    assignee_rule: AssigneeRule
    assignee_user_id: uuid.UUID | None = None
    assignee_org_unit_id: uuid.UUID | None = None
    position_code: str | None = Field(default=None, max_length=64)
    role: UserRole | None = None
    deadline_hours: int | None = Field(default=None, ge=1, le=24 * 30)
    is_mandatory: bool = True
    requires_kep: bool = False

    @model_validator(mode="after")
    def _rule_params(self) -> RouteTemplateStepIn:
        r = self.assignee_rule
        if r is AssigneeRule.SPECIFIC_USER and self.assignee_user_id is None:
            raise ValueError("specific_user потребує assignee_user_id")
        if r is AssigneeRule.POSITION_IN_UNIT and not (self.assignee_org_unit_id and self.position_code):
            raise ValueError("position_in_unit потребує assignee_org_unit_id і position_code")
        if r is AssigneeRule.UNIT_HEAD and self.assignee_org_unit_id is None:
            raise ValueError("unit_head потребує assignee_org_unit_id")
        if r in {AssigneeRule.ROLE, AssigneeRule.AUTHOR_UNIT_ROLE} and self.role is None:
            raise ValueError(f"{r.value} потребує role")
        if self.step_kind is StepKind.SIGNING:
            self.requires_kep = True
        return self


class RouteTemplateCreate(InputModel):
    code: str = Field(max_length=64, pattern=r"^[A-Z0-9_]+$")
    name_uk: NonEmptyStr = Field(max_length=256)
    description: str | None = None
    document_type_id: uuid.UUID
    org_unit_id: uuid.UUID | None = None
    steps: list[RouteTemplateStepIn] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _steps(self) -> RouteTemplateCreate:
        keys = [(s.stage_no, s.ordinal) for s in self.steps]
        if len(keys) != len(set(keys)):
            raise ValueError("Пари (stage_no, ordinal) кроків мають бути унікальними")
        signing = [s.stage_no for s in self.steps if s.step_kind is StepKind.SIGNING]
        later = [s.stage_no for s in self.steps
                 if s.step_kind not in {StepKind.ACKNOWLEDGEMENT, StepKind.REGISTRATION}]
        if signing and max(later) > max(signing):
            raise ValueError("Візування й погодження мають передувати підписанню")
        return self


class ApprovalStepIn(InputModel):
    """Крок маршруту, зібраний вручну або прийнятий з пропозиції ШІ."""

    stage_no: int = Field(ge=1)
    ordinal: int = Field(ge=1)
    step_kind: StepKind
    assignee_id: uuid.UUID
    acting_role: UserRole | None = None
    is_mandatory: bool = True
    deadline_hours: int | None = Field(default=None, ge=1, le=24 * 30)


class ApprovalRouteCreate(InputModel):
    document_version_id: uuid.UUID
    origin: RouteOrigin
    template_id: uuid.UUID | None = None
    suggested_by_ai_run_id: uuid.UUID | None = None
    steps: list[ApprovalStepIn] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def _origin(self) -> ApprovalRouteCreate:
        if self.origin is RouteOrigin.TEMPLATE and self.template_id is None:
            raise ValueError("Маршрут із шаблону потребує template_id")
        if self.origin is RouteOrigin.AI_SUGGESTED and self.suggested_by_ai_run_id is None:
            raise ValueError("Маршрут від ШІ потребує suggested_by_ai_run_id (людина підтверджує пропозицію)")
        if self.origin is not RouteOrigin.TEMPLATE and not self.steps:
            raise ValueError("Ручний маршрут або маршрут від ШІ потребує кроків")
        return self


class StepDecision(InputModel):
    """Рішення людини по кроку. Підписання (КЕП) йде окремим ендпоінтом з контейнером підпису."""

    decision: Literal[StepStatus.APPROVED, StepStatus.APPROVED_WITH_REMARKS, StepStatus.REJECTED]
    comment: str | None = Field(default=None, max_length=10_000)

    @model_validator(mode="after")
    def _comment(self) -> StepDecision:
        if self.decision is not StepStatus.APPROVED and not (self.comment and self.comment.strip()):
            raise ValueError("Зауваження або причина відхилення обов'язкові")
        return self


class ApprovalStepRead(ReadModel):
    id: uuid.UUID
    route_id: uuid.UUID
    stage_no: int
    ordinal: int
    step_kind: StepKind
    assignee_id: uuid.UUID
    original_assignee_id: uuid.UUID | None
    acting_role: UserRole | None
    status: StepStatus
    is_mandatory: bool
    requires_kep: bool
    activated_at: datetime | None
    due_at: datetime | None
    decided_at: datetime | None
    decision_comment: str | None


class ApprovalRouteRead(ReadModel):
    id: uuid.UUID
    document_id: uuid.UUID
    document_version_id: uuid.UUID
    template_id: uuid.UUID | None
    origin: RouteOrigin
    suggested_by_ai_run_id: uuid.UUID | None
    status: RouteStatus
    current_stage: int | None
    initiated_by_id: uuid.UUID
    started_at: datetime | None
    completed_at: datetime | None
    steps: list[ApprovalStepRead] = Field(default_factory=list)


class ApprovalStepHistoryRead(ReadModel):
    id: int
    step_id: uuid.UUID
    route_id: uuid.UUID
    document_id: uuid.UUID
    from_status: StepStatus | None
    to_status: StepStatus
    assignee_id: uuid.UUID
    acting_role: UserRole | None
    actor_type: AuditActorType
    changed_by_id: uuid.UUID | None
    comment: str | None
    changed_at: datetime
