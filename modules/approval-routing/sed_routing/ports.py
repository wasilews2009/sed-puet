"""Порти інфраструктури. Домен залежить лише від них; SQL-, MinIO- і КЕП-адаптери
підміняються в тестах і замінюються без зміни логіки маршрутизації."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Protocol

from sed_db.enums import UserRole

from .domain.models import DelegationInfo, Holder, UnitInfo


class OrgDirectory(Protocol):
    """Оргструктура і ролі на дату (чинні призначення, лише активні користувачі)."""

    async def unit_chain(self, unit_id: uuid.UUID) -> list[UnitInfo]:
        """Підрозділ і всі його предки, від найближчого до університету."""
        ...

    async def role_holders(self, role: UserRole, unit_id: uuid.UUID | None, on: date,
                           *, any_unit: bool = False) -> list[Holder]:
        """Власники ролі в підрозділі (`unit_id=None` — університетський рівень)."""
        ...

    async def position_holders(self, unit_id: uuid.UUID, position_code: str, on: date) -> list[Holder]: ...

    async def user(self, user_id: uuid.UUID) -> Holder | None: ...

    async def active_delegation(self, user_id: uuid.UUID, document_type_id: uuid.UUID,
                                at: datetime) -> DelegationInfo | None: ...


class SignatureStore(Protocol):
    """Сховище відокремлених контейнерів КЕП (MinIO, бакет із versioning і object lock)."""

    bucket: str

    async def put(self, key: str, data: bytes, metadata: dict[str, str]) -> None: ...

    async def delete(self, key: str) -> None:
        """Компенсація, якщо транзакція в БД не вдалася."""
        ...


class DocumentContentReader(Protocol):
    """Вміст версії документа. Потрібен лише для підписів ДСТУ 4145: їхній геш (ГОСТ 34.311 /
    ДСТУ 7564) рахує сертифікований провайдер над самим файлом, а не над нашим SHA-256."""

    async def read(self, bucket: str, key: str, version_id: str | None) -> bytes: ...
