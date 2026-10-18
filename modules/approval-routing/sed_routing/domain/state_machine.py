"""Чиста (без БД) логіка переходів маршруту погодження.

Інваріанти:
* рішення приймається лише по кроку в статусі `active`, і лише кроки поточного етапу активні;
* наступний етап активується тільки тоді, коли всі обов'язкові кроки поточного мають позитивне
  рішення (`approved`, `approved_with_remarks`) або пропущені системою (`skipped`);
* відхилення обов'язкового кроку завершує маршрут статусом `rejected` (документ — на
  доопрацювання); відхилення необов'язкового — лише зауваження, маршрут іде далі;
* коли етап пройдено, незакриті необов'язкові кроки цього етапу закриваються як `skipped`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from sed_db.enums import RouteStatus, StepKind, StepStatus, UserRole

from .errors import AccessDenied, InvalidState

POSITIVE: frozenset[StepStatus] = frozenset(
    {StepStatus.APPROVED, StepStatus.APPROVED_WITH_REMARKS, StepStatus.SKIPPED})
HUMAN_DECISIONS: frozenset[StepStatus] = frozenset(
    {StepStatus.APPROVED, StepStatus.APPROVED_WITH_REMARKS, StepStatus.REJECTED})
CLOSED: frozenset[StepStatus] = POSITIVE | {
    StepStatus.REJECTED, StepStatus.DELEGATED, StepStatus.EXPIRED}


@dataclass
class StepState:
    id: uuid.UUID
    stage_no: int
    ordinal: int
    step_kind: StepKind
    assignee_id: uuid.UUID
    status: StepStatus = StepStatus.WAITING
    is_mandatory: bool = True
    requires_kep: bool = False
    acting_role: UserRole | None = None
    template_step_id: uuid.UUID | None = None
    original_assignee_id: uuid.UUID | None = None
    delegation_id: uuid.UUID | None = None
    activated_at: datetime | None = None
    due_at: datetime | None = None
    decided_at: datetime | None = None
    decision_comment: str | None = None
    dirty: bool = field(default=False, compare=False)

    def set(self, **changes: object) -> None:
        for k, v in changes.items():
            setattr(self, k, v)
        self.dirty = True


@dataclass
class RouteState:
    id: uuid.UUID
    document_id: uuid.UUID
    document_version_id: uuid.UUID
    status: RouteStatus
    initiated_by_id: uuid.UUID
    steps: list[StepState]
    current_stage: int | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    cancel_reason: str | None = None
    template_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        self.steps.sort(key=lambda s: (s.stage_no, s.ordinal))

    @property
    def stages(self) -> list[int]:
        return sorted({s.stage_no for s in self.steps})

    def stage_steps(self, stage: int | None) -> list[StepState]:
        return [s for s in self.steps if s.stage_no == stage]

    def step(self, step_id: uuid.UUID) -> StepState:
        for s in self.steps:
            if s.id == step_id:
                return s
        raise InvalidState("Крок не належить цьому маршруту", step_id=str(step_id))


class Next(StrEnum):
    WAIT = "wait"
    ACTIVATE = "activate"
    COMPLETE = "complete"
    REJECT = "reject"


def evaluate(route: RouteState) -> tuple[Next, int | None]:
    """Що має статися з маршрутом далі, з огляду на рішення поточного етапу."""
    if route.status is not RouteStatus.ACTIVE:
        return Next.WAIT, None
    if route.current_stage is None:
        return (Next.ACTIVATE, route.stages[0]) if route.stages else (Next.COMPLETE, None)
    current = route.stage_steps(route.current_stage)
    mandatory = [s for s in current if s.is_mandatory]
    if any(s.status is StepStatus.REJECTED for s in mandatory):
        return Next.REJECT, route.current_stage
    if any(s.status in {StepStatus.EXPIRED} for s in mandatory):
        return Next.WAIT, None  # прострочений обов'язковий крок вирішує ініціатор (перепризначення)
    if not all(s.status in POSITIVE for s in mandatory):
        return Next.WAIT, None
    later = [st for st in route.stages if st > route.current_stage]
    return (Next.ACTIVATE, later[0]) if later else (Next.COMPLETE, None)


def start(route: RouteState, now: datetime) -> None:
    if route.status is not RouteStatus.DRAFT:
        raise InvalidState(f"Маршрут уже в статусі {route.status.value}")
    if not route.steps:
        raise InvalidState("Маршрут без жодного кроку не можна запустити")
    route.status, route.started_at, route.current_stage = RouteStatus.ACTIVE, now, None


def ensure_can_decide(route: RouteState, step: StepState, actor_id: uuid.UUID) -> None:
    if route.status is not RouteStatus.ACTIVE:
        raise InvalidState(f"Маршрут не активний (статус {route.status.value})")
    if step.status is StepStatus.WAITING:
        raise InvalidState("Етап ще не настав: попередні учасники не завершили візування",
                           current_stage=route.current_stage, step_stage=step.stage_no)
    if step.status is not StepStatus.ACTIVE:
        raise InvalidState(f"Рішення по кроку вже прийнято ({step.status.value})")
    if step.stage_no != route.current_stage:  # захист від неузгоджених даних
        raise InvalidState("Крок не належить поточному етапу маршруту")
    if step.assignee_id != actor_id:
        raise AccessDenied("Рішення може прийняти лише призначений учасник кроку")


def record_decision(step: StepState, decision: StepStatus, comment: str | None, now: datetime) -> None:
    if decision not in HUMAN_DECISIONS:
        raise InvalidState(f"Недопустиме рішення {decision.value}")
    if decision is not StepStatus.APPROVED and not (comment and comment.strip()):
        raise InvalidState("Зауваження або причина відхилення обов'язкові")
    step.set(status=decision, decided_at=now, decision_comment=comment)


def close_open_steps(route: RouteState, stage: int, now: datetime, reason: str) -> None:
    for s in route.stage_steps(stage):
        if s.status is StepStatus.ACTIVE:
            s.set(status=StepStatus.SKIPPED, decided_at=now, decision_comment=reason)


def finish(route: RouteState, status: RouteStatus, now: datetime, reason: str | None = None) -> None:
    route.status, route.completed_at = status, now
    if status is RouteStatus.CANCELLED:
        route.cancel_reason = reason
        for st in route.stages:
            close_open_steps(route, st, now, reason or "Маршрут скасовано")
