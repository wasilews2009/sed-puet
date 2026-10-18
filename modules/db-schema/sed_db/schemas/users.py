"""Користувачі та їхні ролі."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import EmailStr, Field, computed_field, model_validator

from ..enums import LABELS_UK, UNIT_SCOPED_ROLES, UserRole
from .common import InputModel, NonEmptyStr, ReadModel


class UserRoleAssignmentCreate(InputModel):
    role: UserRole
    org_unit_id: uuid.UUID | None = Field(
        default=None, description="Підрозділ, у межах якого діє роль; NULL — рівень університету"
    )
    scope_note: str | None = Field(default=None, max_length=256,
                                   description="Напрям проректора, академічна група студента тощо")
    is_acting: bool = Field(default=False, description="Тимчасово виконуючий обов'язки")
    valid_from: date
    valid_to: date | None = None
    basis: str | None = Field(default=None, max_length=512, description="Наказ про призначення / зарахування")

    @model_validator(mode="after")
    def _scope(self) -> UserRoleAssignmentCreate:
        if self.role in UNIT_SCOPED_ROLES and self.org_unit_id is None:
            raise ValueError(f"Роль «{LABELS_UK[self.role]}» потребує підрозділу (org_unit_id)")
        if self.role is UserRole.RECTOR and self.org_unit_id is not None:
            raise ValueError("Роль «Ректор» діє на рівні університету, org_unit_id має бути порожнім")
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("valid_to має бути пізніше за valid_from")
        return self


class UserRoleAssignmentRead(ReadModel):
    id: uuid.UUID
    role: UserRole
    org_unit_id: uuid.UUID | None
    scope_note: str | None
    is_acting: bool
    valid_from: date
    valid_to: date | None
    basis: str | None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def role_label_uk(self) -> str:
        label = LABELS_UK[self.role]
        return f"т.в.о. {label.lower()}" if self.is_acting else label


class UserCreate(InputModel):
    keycloak_sub: uuid.UUID
    email: EmailStr
    last_name: NonEmptyStr = Field(max_length=128)
    first_name: NonEmptyStr = Field(max_length=128)
    patronymic: str | None = Field(default=None, max_length=128)
    primary_org_unit_id: uuid.UUID | None = None
    roles: list[UserRoleAssignmentCreate] = Field(default_factory=list)


class UserRead(ReadModel):
    id: uuid.UUID
    keycloak_sub: uuid.UUID
    email: str
    last_name: str
    first_name: str
    patronymic: str | None
    primary_org_unit_id: uuid.UUID | None
    is_active: bool
    created_at: datetime
    roles: list[UserRoleAssignmentRead] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.last_name, self.first_name, self.patronymic) if p)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def signature_name(self) -> str:
        """Ім'я та ПРІЗВИЩЕ для реквізиту «Підпис» (ДСТУ 4163:2020)."""
        return f"{self.first_name} {self.last_name.upper()}"


class UserBrief(ReadModel):
    """Мінімум для списків і маршрутів (без email та інших ПД)."""

    id: uuid.UUID
    last_name: str
    first_name: str
