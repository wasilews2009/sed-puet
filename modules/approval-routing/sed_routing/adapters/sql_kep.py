"""Реєстри ключів у PostgreSQL над таблицями з `schema_proposal/0003_kep_keys.sql`.

Таблиці описано тут (SQLAlchemy Core), доки їх не перенесено в `sed_db` тредом схеми БД.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    Enum,
    LargeBinary,
    MetaData,
    String,
    Table,
    insert,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..kep.certinfo import CertLevel
from ..kep.registry import InternalCertRecord, KeyBinding

metadata = MetaData()
_tz = DateTime(timezone=True)

user_kep_identities = Table(
    "user_kep_identities", metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("user_id", UUID(as_uuid=True), nullable=False),
    Column("level", Enum("qualified", "internal", name="kep_key_level", create_type=False), nullable=False),
    Column("cert_fingerprint_sha256", String(64), nullable=False),
    Column("cert_serial", String(128), nullable=False),
    Column("cert_issuer", String(512), nullable=False),
    Column("cert_subject_cn", String(512), nullable=False),
    Column("cert_valid_from", _tz, nullable=False),
    Column("cert_valid_to", _tz, nullable=False),
    Column("tax_id_hmac", String(64)),
    Column("key_format", String(32)),
    Column("bound_via", String(32), nullable=False),
    Column("bound_at", _tz),
    Column("revoked_at", _tz),
    Column("revoke_reason", String(256)),
)

internal_certificates = Table(
    "internal_certificates", metadata,
    Column("serial_hex", String(64), primary_key=True),
    Column("user_id", UUID(as_uuid=True), nullable=False),
    Column("cert_fingerprint_sha256", String(64), nullable=False),
    Column("cert_der", LargeBinary, nullable=False),
    Column("not_before", _tz, nullable=False),
    Column("not_after", _tz, nullable=False),
    Column("delivery", String(16), nullable=False),
    Column("issued_at", _tz, nullable=False),
    Column("revoked_at", _tz),
    Column("revocation_reason", String(32)),
    Column("revoked_by_id", UUID(as_uuid=True)),
)

_K = user_kep_identities.c
_I = internal_certificates.c


def _binding(row) -> KeyBinding:  # noqa: ANN001
    return KeyBinding(
        id=row.id, user_id=row.user_id, level=CertLevel(row.level), fingerprint_sha256=row.cert_fingerprint_sha256,
        cert_serial=row.cert_serial, cert_issuer=row.cert_issuer, cert_subject_cn=row.cert_subject_cn,
        cert_valid_from=row.cert_valid_from, cert_valid_to=row.cert_valid_to, bound_via=row.bound_via,
        tax_id_hmac=row.tax_id_hmac, key_format=row.key_format, bound_at=row.bound_at,
        revoked_at=row.revoked_at, revoke_reason=row.revoke_reason)


def _record(row) -> InternalCertRecord:  # noqa: ANN001
    return InternalCertRecord(
        serial_hex=row.serial_hex, user_id=row.user_id, fingerprint_sha256=row.cert_fingerprint_sha256,
        cert_der=row.cert_der, not_before=row.not_before, not_after=row.not_after, delivery=row.delivery,
        issued_at=row.issued_at, revoked_at=row.revoked_at, revocation_reason=row.revocation_reason,
        revoked_by_id=row.revoked_by_id)


class _Base:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self.sessionmaker = sessionmaker


class SqlKeyRegistry(_Base):
    async def add(self, b: KeyBinding) -> KeyBinding:
        async with self.sessionmaker() as s, s.begin():
            row = (await s.execute(insert(user_kep_identities).values(
                id=b.id, user_id=b.user_id, level=b.level.value, cert_fingerprint_sha256=b.fingerprint_sha256,
                cert_serial=b.cert_serial, cert_issuer=b.cert_issuer[:512], cert_subject_cn=b.cert_subject_cn[:512],
                cert_valid_from=b.cert_valid_from, cert_valid_to=b.cert_valid_to, tax_id_hmac=b.tax_id_hmac,
                key_format=b.key_format, bound_via=b.bound_via,
            ).returning(*user_kep_identities.c))).one()
        return _binding(row)

    async def get(self, binding_id: uuid.UUID) -> KeyBinding | None:
        async with self.sessionmaker() as s:
            row = (await s.execute(select(user_kep_identities).where(_K.id == binding_id))).first()
        return _binding(row) if row else None

    async def for_user(self, user_id: uuid.UUID, *, active_only: bool = True) -> list[KeyBinding]:
        q = select(user_kep_identities).where(_K.user_id == user_id).order_by(_K.bound_at.desc())
        if active_only:
            q = q.where(_K.revoked_at.is_(None))
        async with self.sessionmaker() as s:
            return [_binding(r) for r in (await s.execute(q)).all()]

    async def by_fingerprint(self, fingerprint: str) -> KeyBinding | None:
        async with self.sessionmaker() as s:
            row = (await s.execute(select(user_kep_identities).where(
                _K.cert_fingerprint_sha256 == fingerprint, _K.revoked_at.is_(None)))).first()
        return _binding(row) if row else None

    async def revoke(self, binding_id: uuid.UUID, at: datetime, reason: str) -> KeyBinding | None:
        async with self.sessionmaker() as s, s.begin():
            row = (await s.execute(update(user_kep_identities)
                                   .where(_K.id == binding_id, _K.revoked_at.is_(None))
                                   .values(revoked_at=at, revoke_reason=reason[:256])
                                   .returning(*user_kep_identities.c))).first()
        return _binding(row) if row else None


class SqlInternalCertRegistry(_Base):
    async def add(self, r: InternalCertRecord) -> None:
        async with self.sessionmaker() as s, s.begin():
            await s.execute(insert(internal_certificates).values(
                serial_hex=r.serial_hex, user_id=r.user_id, cert_fingerprint_sha256=r.fingerprint_sha256,
                cert_der=r.cert_der, not_before=r.not_before, not_after=r.not_after, delivery=r.delivery,
                issued_at=r.issued_at))

    async def get(self, serial_hex: str) -> InternalCertRecord | None:
        async with self.sessionmaker() as s:
            row = (await s.execute(select(internal_certificates).where(_I.serial_hex == serial_hex))).first()
        return _record(row) if row else None

    async def active_for_user(self, user_id: uuid.UUID) -> list[InternalCertRecord]:
        async with self.sessionmaker() as s:
            rows = (await s.execute(select(internal_certificates).where(
                _I.user_id == user_id, _I.revoked_at.is_(None)))).all()
        return [_record(r) for r in rows]

    async def revoke(self, serial_hex: str, at: datetime, reason: str, by: uuid.UUID | None) -> InternalCertRecord | None:
        async with self.sessionmaker() as s, s.begin():
            row = (await s.execute(update(internal_certificates)
                                   .where(_I.serial_hex == serial_hex, _I.revoked_at.is_(None))
                                   .values(revoked_at=at, revocation_reason=reason, revoked_by_id=by)
                                   .returning(*internal_certificates.c))).first()
        return _record(row) if row else None

    async def revoked(self) -> list[InternalCertRecord]:
        async with self.sessionmaker() as s:
            rows = (await s.execute(select(internal_certificates).where(_I.revoked_at.is_not(None)))).all()
        return [_record(r) for r in rows]
