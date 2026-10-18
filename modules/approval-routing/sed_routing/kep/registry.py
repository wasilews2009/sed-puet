"""Реєстри ключів: які сертифікати закріплені за користувачем і які внутрішні сертифікати видано.

Порти + реалізації в пам'яті (тести, пілот). SQL-реалізація — `adapters/sql_kep.py`
над таблицями з `schema_proposal/0003_kep_keys.sql`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Protocol

from .certinfo import CertLevel
from .models import CertificateBinding, RevocationStatus


@dataclass(frozen=True)
class KeyBinding:
    """Закріплення сертифіката за користувачем (після доказу володіння ключем)."""

    user_id: uuid.UUID
    level: CertLevel
    fingerprint_sha256: str
    cert_serial: str
    cert_issuer: str
    cert_subject_cn: str
    cert_valid_from: datetime
    cert_valid_to: datetime
    bound_via: str                       # proof_of_possession | internal_issuance
    tax_id_hmac: str | None = None       # лише для кваліфікованих
    key_format: str | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    bound_at: datetime | None = None
    revoked_at: datetime | None = None
    revoke_reason: str | None = None

    @property
    def active(self) -> bool:
        return self.revoked_at is None


@dataclass(frozen=True)
class InternalCertRecord:
    serial_hex: str
    user_id: uuid.UUID
    fingerprint_sha256: str
    cert_der: bytes
    not_before: datetime
    not_after: datetime
    delivery: str                        # csr | server_p12
    issued_at: datetime
    revoked_at: datetime | None = None
    revocation_reason: str | None = None  # RFC 5280: key_compromise, superseded, cessation_of_operation …
    revoked_by_id: uuid.UUID | None = None


class KeyRegistry(Protocol):
    async def add(self, b: KeyBinding) -> KeyBinding: ...

    async def get(self, binding_id: uuid.UUID) -> KeyBinding | None: ...

    async def for_user(self, user_id: uuid.UUID, *, active_only: bool = True) -> list[KeyBinding]: ...

    async def by_fingerprint(self, fingerprint: str) -> KeyBinding | None:
        """Активне закріплення за відбитком сертифіката."""
        ...

    async def revoke(self, binding_id: uuid.UUID, at: datetime, reason: str) -> KeyBinding | None: ...


class InternalCertRegistry(Protocol):
    async def add(self, r: InternalCertRecord) -> None: ...

    async def get(self, serial_hex: str) -> InternalCertRecord | None: ...

    async def active_for_user(self, user_id: uuid.UUID) -> list[InternalCertRecord]: ...

    async def revoke(self, serial_hex: str, at: datetime, reason: str, by: uuid.UUID | None) -> InternalCertRecord | None: ...

    async def revoked(self) -> list[InternalCertRecord]: ...


class InMemoryKeyRegistry:
    def __init__(self) -> None:
        self.items: dict[uuid.UUID, KeyBinding] = {}

    async def add(self, b: KeyBinding) -> KeyBinding:
        if await self.by_fingerprint(b.fingerprint_sha256):
            raise ValueError("Сертифікат уже закріплено")
        self.items[b.id] = b
        return b

    async def get(self, binding_id: uuid.UUID) -> KeyBinding | None:
        return self.items.get(binding_id)

    async def for_user(self, user_id: uuid.UUID, *, active_only: bool = True) -> list[KeyBinding]:
        return [b for b in self.items.values() if b.user_id == user_id and (b.active or not active_only)]

    async def by_fingerprint(self, fingerprint: str) -> KeyBinding | None:
        return next((b for b in self.items.values() if b.fingerprint_sha256 == fingerprint and b.active), None)

    async def revoke(self, binding_id: uuid.UUID, at: datetime, reason: str) -> KeyBinding | None:
        b = self.items.get(binding_id)
        if b is None or not b.active:
            return None
        self.items[binding_id] = replace(b, revoked_at=at, revoke_reason=reason)
        return self.items[binding_id]


class InMemoryInternalCertRegistry:
    def __init__(self) -> None:
        self.items: dict[str, InternalCertRecord] = {}

    async def add(self, r: InternalCertRecord) -> None:
        self.items[r.serial_hex] = r

    async def get(self, serial_hex: str) -> InternalCertRecord | None:
        return self.items.get(serial_hex)

    async def active_for_user(self, user_id: uuid.UUID) -> list[InternalCertRecord]:
        return [r for r in self.items.values() if r.user_id == user_id and r.revoked_at is None]

    async def revoke(self, serial_hex: str, at: datetime, reason: str, by: uuid.UUID | None) -> InternalCertRecord | None:
        r = self.items.get(serial_hex)
        if r is None or r.revoked_at is not None:
            return None
        self.items[serial_hex] = replace(r, revoked_at=at, revocation_reason=reason, revoked_by_id=by)
        return self.items[serial_hex]

    async def revoked(self) -> list[InternalCertRecord]:
        return [r for r in self.items.values() if r.revoked_at is not None]


class RegistryBinding:
    """Порт CertificateBinding поверх реєстру: сертифікат закріплений за цим користувачем?"""

    def __init__(self, registry: KeyRegistry) -> None:
        self.registry = registry

    async def matches(self, user_id: uuid.UUID, fingerprint: str, tax_id_hmac: str | None) -> bool | None:
        b = await self.registry.by_fingerprint(fingerprint)
        if b is not None:
            return b.user_id == user_id
        mine = [x for x in await self.registry.for_user(user_id) if x.level is CertLevel.QUALIFIED]
        if not mine:
            return None
        # Новий сертифікат тієї ж особи (перевипуск у КНЕДП) — за РНОКПП; інакше чужий.
        return tax_id_hmac is not None and any(x.tax_id_hmac == tax_id_hmac for x in mine)


class FirstMatchBinding:
    """Кілька джерел закріплень (напр., реєстр у БД + імітаційні ключі dev): перше, що знає користувача."""

    def __init__(self, *sources: CertificateBinding) -> None:
        self.sources = sources

    async def matches(self, user_id: uuid.UUID, fingerprint: str, tax_id_hmac: str | None) -> bool | None:
        for src in self.sources:
            r = await src.matches(user_id, fingerprint, tax_id_hmac)
            if r is not None:
                return r
        return None


class InternalRevocation:
    """Статус внутрішнього сертифіката — з реєстру видачі (власний «OCSP»)."""

    def __init__(self, registry: InternalCertRegistry) -> None:
        self.registry = registry

    async def status_by_serial(self, serial_hex: str, user_id: uuid.UUID | None) -> RevocationStatus:
        r = await self.registry.get(serial_hex)
        if r is None or (user_id is not None and r.user_id != user_id):
            return RevocationStatus.UNKNOWN
        return RevocationStatus.REVOKED if r.revoked_at else RevocationStatus.GOOD
