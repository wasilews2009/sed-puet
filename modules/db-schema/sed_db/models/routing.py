"""Маршрути погодження: шаблони (типові маршрути), екземпляри для документів
та незмінна історія статусів погодження."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Identity,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, TimestampMixin, UUIDPkMixin, pg_enum
from ..enums import AssigneeRule, AuditActorType, DocumentStatus, RouteOrigin, RouteStatus, StepKind, StepStatus, UserRole

DECIDED_STATUSES = "('approved','approved_with_remarks','rejected','skipped','delegated','expired')"


class RouteTemplate(UUIDPkMixin, TimestampMixin, Base):
    """Типовий маршрут для виду документа (і, за потреби, підрозділу).

    Шаблони версіонуються: зміна маршруту = новий рядок з `version + 1`,
    запущені маршрути продовжують посилатися на стару версію.
    """

    __tablename__ = "route_templates"

    code: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    name_uk: Mapped[str] = mapped_column(String(256))
    description: Mapped[str | None] = mapped_column(Text)
    document_type_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_types.id", ondelete="RESTRICT"), index=True
    )
    org_unit_id: Mapped[uuid.UUID | None] = mapped_column(  # NULL — для всього університету
        ForeignKey("org_units.id", ondelete="RESTRICT")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))

    steps: Mapped[list[RouteTemplateStep]] = relationship(
        back_populates="template",
        order_by="(RouteTemplateStep.stage_no, RouteTemplateStep.ordinal)",
        lazy="raise",
    )

    __table_args__ = (
        UniqueConstraint("code", "version"),
        CheckConstraint("version > 0", name="version_positive"),
    )


class RouteTemplateStep(UUIDPkMixin, Base):
    """Крок шаблону. Кроки з однаковим `stage_no` виконуються паралельно."""

    __tablename__ = "route_template_steps"

    template_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("route_templates.id", ondelete="CASCADE"), index=True
    )
    stage_no: Mapped[int] = mapped_column(SmallInteger)
    ordinal: Mapped[int] = mapped_column(SmallInteger)
    step_kind: Mapped[StepKind] = mapped_column(pg_enum(StepKind, "step_kind"))
    assignee_rule: Mapped[AssigneeRule] = mapped_column(pg_enum(AssigneeRule, "assignee_rule"))
    assignee_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    assignee_org_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("org_units.id", ondelete="RESTRICT")
    )
    position_code: Mapped[str | None] = mapped_column(String(64))
    role: Mapped[UserRole | None] = mapped_column(pg_enum(UserRole, "user_role"))
    deadline_hours: Mapped[int | None] = mapped_column(Integer)  # робочі години на крок
    is_mandatory: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    requires_kep: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))

    template: Mapped[RouteTemplate] = relationship(back_populates="steps", lazy="raise")

    __table_args__ = (
        UniqueConstraint("template_id", "stage_no", "ordinal"),
        CheckConstraint("stage_no > 0 AND ordinal > 0", name="positive_order"),
        CheckConstraint("deadline_hours IS NULL OR deadline_hours > 0", name="deadline_positive"),
        CheckConstraint(
            "(assignee_rule <> 'specific_user' OR assignee_user_id IS NOT NULL) AND "
            "(assignee_rule <> 'position_in_unit' OR (assignee_org_unit_id IS NOT NULL AND position_code IS NOT NULL)) AND "
            "(assignee_rule <> 'unit_head' OR assignee_org_unit_id IS NOT NULL) AND "
            "(assignee_rule NOT IN ('role','author_unit_role') OR role IS NOT NULL)",
            name="assignee_rule_params",
        ),
        CheckConstraint("step_kind <> 'signing' OR requires_kep", name="signing_requires_kep"),
    )


class ApprovalRoute(UUIDPkMixin, TimestampMixin, Base):
    """Екземпляр маршруту для конкретної (заблокованої) версії документа."""

    __tablename__ = "approval_routes"

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="RESTRICT"), index=True
    )
    document_version_id: Mapped[uuid.UUID] = mapped_column(index=True)
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("route_templates.id", ondelete="RESTRICT")
    )
    origin: Mapped[RouteOrigin] = mapped_column(pg_enum(RouteOrigin, "route_origin"))
    # Якщо маршрут запропонував ШІ — запуск, з якого взято пропозицію.
    suggested_by_ai_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ai_runs.id", ondelete="RESTRICT")
    )
    status: Mapped[RouteStatus] = mapped_column(
        pg_enum(RouteStatus, "route_status"), server_default=RouteStatus.DRAFT.value
    )
    current_stage: Mapped[int | None] = mapped_column(SmallInteger)
    initiated_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    started_at: Mapped[datetime | None]
    completed_at: Mapped[datetime | None]
    cancel_reason: Mapped[str | None] = mapped_column(String(1024))

    steps: Mapped[list[ApprovalStep]] = relationship(
        back_populates="route",
        order_by="(ApprovalStep.stage_no, ApprovalStep.ordinal)",
        lazy="raise",
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["document_version_id", "document_id"],
            ["document_versions.id", "document_versions.document_id"],
            name="fk_approval_routes_version_same_document",
            ondelete="RESTRICT",
        ),
        # Одночасно не більше одного незавершеного маршруту на документ.
        Index(
            "uq_approval_routes_one_open_per_document",
            "document_id",
            unique=True,
            postgresql_where=text("status IN ('draft','active')"),
        ),
        CheckConstraint(
            "origin <> 'ai_suggested' OR suggested_by_ai_run_id IS NOT NULL",
            name="ai_origin_has_run",
        ),
        CheckConstraint("origin <> 'template' OR template_id IS NOT NULL", name="template_origin"),
        CheckConstraint("status <> 'active' OR started_at IS NOT NULL", name="active_started"),
        CheckConstraint(
            "status NOT IN ('completed','rejected','cancelled') OR completed_at IS NOT NULL",
            name="finished_has_time",
        ),
        CheckConstraint("status <> 'cancelled' OR cancel_reason IS NOT NULL", name="cancel_reason"),
    )


class ApprovalStep(UUIDPkMixin, TimestampMixin, Base):
    """Конкретне завдання на візу/підпис конкретній людині.

    `activated_at` / `decided_at` / `due_at` — сировина для виявлення затримок
    і аномалій у візуванні.
    """

    __tablename__ = "approval_steps"

    route_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("approval_routes.id", ondelete="CASCADE"), index=True
    )
    template_step_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("route_template_steps.id", ondelete="SET NULL")
    )
    stage_no: Mapped[int] = mapped_column(SmallInteger)
    ordinal: Mapped[int] = mapped_column(SmallInteger)
    step_kind: Mapped[StepKind] = mapped_column(pg_enum(StepKind, "step_kind"))
    assignee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    # Якщо крок перейшов до заступника — кому він був призначений спершу.
    original_assignee_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    delegation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("delegations.id", ondelete="RESTRICT")
    )
    # У якій якості людина візує (напр. «проректор», а не «викладач»).
    acting_role: Mapped[UserRole | None] = mapped_column(pg_enum(UserRole, "user_role"))
    status: Mapped[StepStatus] = mapped_column(
        pg_enum(StepStatus, "step_status"), server_default=StepStatus.WAITING.value
    )
    is_mandatory: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    requires_kep: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    activated_at: Mapped[datetime | None]
    due_at: Mapped[datetime | None]
    decided_at: Mapped[datetime | None]
    decision_comment: Mapped[str | None] = mapped_column(Text)  # зауваження / причина відхилення

    route: Mapped[ApprovalRoute] = relationship(back_populates="steps", lazy="raise")

    __table_args__ = (
        UniqueConstraint("route_id", "stage_no", "ordinal"),
        CheckConstraint("stage_no > 0 AND ordinal > 0", name="positive_order"),
        CheckConstraint(
            f"(status IN {DECIDED_STATUSES}) = (decided_at IS NOT NULL)", name="decided_has_time"
        ),
        CheckConstraint(
            "status NOT IN ('rejected','approved_with_remarks') OR "
            "length(btrim(coalesce(decision_comment, ''))) > 0",
            name="rejection_has_comment",
        ),
        CheckConstraint("status IN ('waiting','skipped') OR activated_at IS NOT NULL", name="activated_has_time"),
        CheckConstraint(
            "(delegation_id IS NULL) = (original_assignee_id IS NULL)", name="delegation_consistent"
        ),
        CheckConstraint("step_kind <> 'signing' OR requires_kep", name="signing_requires_kep"),
        # Черга «мої задачі» та моніторинг прострочень.
        Index("ix_approval_steps_inbox", "assignee_id", "status",
              postgresql_where=text("status = 'active'")),
        Index("ix_approval_steps_overdue", "due_at",
              postgresql_where=text("status = 'active' AND due_at IS NOT NULL")),
    )


class DocumentStatusHistory(Base):
    """Кожна зміна статусу документа (заповнює тригер, таблиця append-only).

    Причину переходу застосунок передає через `set_config('sed.change_reason', ..., true)`.
    """

    __tablename__ = "document_status_history"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="RESTRICT"), index=True
    )
    from_status: Mapped[DocumentStatus | None] = mapped_column(
        pg_enum(DocumentStatus, "document_status")
    )
    to_status: Mapped[DocumentStatus] = mapped_column(pg_enum(DocumentStatus, "document_status"))
    document_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_versions.id", ondelete="RESTRICT")
    )
    route_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("approval_routes.id", ondelete="RESTRICT")
    )
    actor_type: Mapped[AuditActorType] = mapped_column(pg_enum(AuditActorType, "audit_actor_type"))
    changed_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    reason: Mapped[str | None] = mapped_column(Text)
    changed_at: Mapped[datetime] = mapped_column(server_default=text("clock_timestamp()"))

    __table_args__ = (
        CheckConstraint("from_status IS DISTINCT FROM to_status", name="real_transition"),
        Index("ix_document_status_history_timeline", "document_id", "changed_at"),
    )


class ApprovalStepHistory(Base):
    """Кожна зміна статусу кроку візування: хто, коли, з яким рішенням і коментарем.

    Разом з `approval_steps.activated_at/decided_at` — сировина для виявлення затримок.
    """

    __tablename__ = "approval_step_history"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    step_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("approval_steps.id", ondelete="RESTRICT"), index=True
    )
    route_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("approval_routes.id", ondelete="RESTRICT"), index=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="RESTRICT"))
    from_status: Mapped[StepStatus | None] = mapped_column(pg_enum(StepStatus, "step_status"))
    to_status: Mapped[StepStatus] = mapped_column(pg_enum(StepStatus, "step_status"))
    assignee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    acting_role: Mapped[UserRole | None] = mapped_column(pg_enum(UserRole, "user_role"))
    actor_type: Mapped[AuditActorType] = mapped_column(pg_enum(AuditActorType, "audit_actor_type"))
    changed_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    comment: Mapped[str | None] = mapped_column(Text)
    changed_at: Mapped[datetime] = mapped_column(server_default=text("clock_timestamp()"))

    __table_args__ = (
        CheckConstraint("from_status IS DISTINCT FROM to_status", name="real_transition"),
        Index("ix_approval_step_history_document", "document_id", "changed_at"),
    )
