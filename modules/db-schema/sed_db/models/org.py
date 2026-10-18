"""Оргструктура ПУЕТ, користувачі (дзеркало Keycloak), функціональні ролі, посади, заміщення."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, TimestampMixin, UUIDPkMixin, pg_enum
from ..enums import EXCLUSIVE_ROLES, UNIT_SCOPED_ROLES, OrgUnitKind, UserRole


class OrgUnit(UUIDPkMixin, TimestampMixin, Base):
    """Структурний підрозділ (ієрархія: університет → інститут → кафедра)."""

    __tablename__ = "org_units"

    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("org_units.id", ondelete="RESTRICT"), index=True
    )
    kind: Mapped[OrgUnitKind] = mapped_column(pg_enum(OrgUnitKind, "org_unit_kind"))
    code: Mapped[str] = mapped_column(String(32), unique=True)  # індекс підрозділу для номенклатури
    name_uk: Mapped[str] = mapped_column(String(512))
    short_name_uk: Mapped[str | None] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    valid_from: Mapped[date] = mapped_column(server_default=func.current_date())
    valid_to: Mapped[date | None]

    parent: Mapped[OrgUnit | None] = relationship(remote_side="OrgUnit.id", lazy="raise")

    __table_args__ = (
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="not_self_parent"),
        CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="valid_period"),
    )


class User(UUIDPkMixin, TimestampMixin, Base):
    """Локальна проєкція облікового запису Keycloak.

    Автентифікація й ролі — у Keycloak; тут лише те, що потрібно для зв'язків
    і маршрутизації. ПІБ — персональні дані: не передається в LLM без маскування.
    """

    __tablename__ = "users"

    keycloak_sub: Mapped[uuid.UUID] = mapped_column(unique=True)
    email: Mapped[str] = mapped_column(String(320))
    last_name: Mapped[str] = mapped_column(String(128))
    first_name: Mapped[str] = mapped_column(String(128))
    patronymic: Mapped[str | None] = mapped_column(String(128))
    primary_org_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("org_units.id", ondelete="SET NULL"), index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    last_synced_at: Mapped[datetime | None]

    roles: Mapped[list[UserRoleAssignment]] = relationship(
        back_populates="user", foreign_keys="UserRoleAssignment.user_id", lazy="raise"
    )

    __table_args__ = (Index("uq_users_email_lower", func.lower(email), unique=True),)

    @property
    def short_name(self) -> str:
        """Формат для підпису за ДСТУ 4163: «Ім'я ПРІЗВИЩЕ»."""
        return f"{self.first_name} {self.last_name.upper()}"


_UNIT_SCOPED_SQL = "(" + ",".join(f"'{r.value}'" for r in sorted(UNIT_SCOPED_ROLES)) + ")"
_EXCLUSIVE_SQL = "(" + ",".join(f"'{r.value}'" for r in sorted(EXCLUSIVE_ROLES)) + ")"


class UserRoleAssignment(UUIDPkMixin, TimestampMixin, Base):
    """Роль користувача з областю дії та строком.

    Одна людина може мати кілька ролей (викладач і завідувач кафедри; студент і голова
    студентського самоврядування). Права доступу й маршрутизація візування читаються звідси;
    `staff_assignments` — кадрові посади зі штатного розпису, вони не дають прав у СЕД.
    """

    __tablename__ = "user_roles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    role: Mapped[UserRole] = mapped_column(pg_enum(UserRole, "user_role"))
    # NULL — університетський рівень (ректор, проректор, голова студсамоврядування).
    org_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("org_units.id", ondelete="RESTRICT"), index=True
    )
    # Для проректора: напрям («з науково-педагогічної роботи»), для студента — група.
    scope_note: Mapped[str | None] = mapped_column(String(256))
    is_acting: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))  # т.в.о.
    valid_from: Mapped[date] = mapped_column(server_default=func.current_date())
    valid_to: Mapped[date | None]
    basis: Mapped[str | None] = mapped_column(String(512))  # наказ про призначення / зарахування
    granted_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    keycloak_synced_at: Mapped[datetime | None]

    user: Mapped[User] = relationship(back_populates="roles", foreign_keys=[user_id], lazy="raise")

    __table_args__ = (
        CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="valid_period"),
        CheckConstraint(
            f"role NOT IN {_UNIT_SCOPED_SQL} OR org_unit_id IS NOT NULL", name="unit_scope_required"
        ),
        CheckConstraint("role <> 'rector' OR org_unit_id IS NULL", name="rector_university_level"),
        # Без дублікатів однієї ролі в тому самому підрозділі на перетинних періодах.
        ExcludeConstraint(
            ("user_id", "="),
            ("role", "="),
            (text("coalesce(org_unit_id, '00000000-0000-0000-0000-000000000000'::uuid)"), "="),
            (text("daterange(valid_from, valid_to, '[)')"), "&&"),
            using="gist",
            name="ex_user_roles_no_duplicates",
        ),
        # Один керівник на підрозділ (і один ректор на університет) у кожен момент часу.
        ExcludeConstraint(
            ("role", "="),
            (text("coalesce(org_unit_id, '00000000-0000-0000-0000-000000000000'::uuid)"), "="),
            (text("daterange(valid_from, valid_to, '[)')"), "&&"),
            where=text(f"role IN {_EXCLUSIVE_SQL}"),
            using="gist",
            name="ex_user_roles_single_head",
        ),
        Index("ix_user_roles_role_unit", "role", "org_unit_id"),
    )


class StaffAssignment(UUIDPkMixin, TimestampMixin, Base):
    """Кадрова посада у підрозділі зі штатного розпису (одна людина може мати кілька посад)."""

    __tablename__ = "staff_assignments"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    org_unit_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("org_units.id", ondelete="RESTRICT"), index=True
    )
    position_code: Mapped[str] = mapped_column(String(64))  # код посади зі штатного розпису
    position_title_uk: Mapped[str] = mapped_column(String(256))
    is_head: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    is_acting: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))  # т.в.о.
    valid_from: Mapped[date]
    valid_to: Mapped[date | None]

    __table_args__ = (
        CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="valid_period"),
        # Не більше одного керівника підрозділу в кожен момент часу.
        ExcludeConstraint(
            ("org_unit_id", "="),
            (text("daterange(valid_from, valid_to, '[)')"), "&&"),
            where=text("is_head"),
            using="gist",
            name="ex_staff_assignments_one_head",
        ),
    )


class Delegation(UUIDPkMixin, TimestampMixin, Base):
    """Заміщення на час відпустки/відрядження: візи переходять до заступника."""

    __tablename__ = "delegations"

    from_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    to_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    # NULL — заміщення за всіма типами документів.
    document_type_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_types.id", ondelete="RESTRICT")
    )
    valid_from: Mapped[datetime]
    valid_to: Mapped[datetime]
    basis: Mapped[str | None] = mapped_column(String(512))  # підстава: наказ про відпустку тощо
    created_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    revoked_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint("from_user_id <> to_user_id", name="not_self"),
        CheckConstraint("valid_to > valid_from", name="valid_period"),
    )
