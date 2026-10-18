"""Кваліфіковані електронні підписи (КЕП / Дія.Підпис) над версіями документів."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base, UUIDPkMixin, pg_enum
from ..enums import SignatureFormat, SignatureProvider, SignatureValidation
from .documents import SHA256_HEX_CHECK


class Signature(UUIDPkMixin, Base):
    """Накладений КЕП. Саме цей запис, а не рішення ШІ, надає документу юридичної сили.

    Підпис зберігається як окремий (detached) контейнер у MinIO. Тригер перевіряє,
    що `signed_content_sha256` збігається з хешем підписуваної версії, і блокує
    версію від змін. Рядок не можна видалити; змінюються лише поля валідації.
    """

    __tablename__ = "signatures"

    document_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="RESTRICT"), index=True
    )
    signer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    approval_step_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("approval_steps.id", ondelete="RESTRICT"), index=True
    )
    signature_format: Mapped[SignatureFormat] = mapped_column(
        pg_enum(SignatureFormat, "signature_format")
    )
    provider: Mapped[SignatureProvider] = mapped_column(pg_enum(SignatureProvider, "signature_provider"))

    storage_bucket: Mapped[str] = mapped_column(String(63))
    storage_key: Mapped[str] = mapped_column(String(1024))
    signed_content_sha256: Mapped[str] = mapped_column(String(64))

    # Дані сертифіката. РНОКПП підписувача — лише як HMAC (персональні дані).
    cert_serial: Mapped[str] = mapped_column(String(128))
    cert_issuer: Mapped[str] = mapped_column(String(512))  # КНЕДП
    cert_subject_cn: Mapped[str] = mapped_column(String(512))
    signer_tax_id_hmac: Mapped[str | None] = mapped_column(String(64))
    cert_valid_from: Mapped[datetime]
    cert_valid_to: Mapped[datetime]

    signed_at: Mapped[datetime]                 # час підпису з контейнера
    tsp_time: Mapped[datetime | None]           # кваліфікована позначка часу
    validation_status: Mapped[SignatureValidation] = mapped_column(
        pg_enum(SignatureValidation, "signature_validation"),
        server_default=SignatureValidation.PENDING.value,
    )
    validation_report: Mapped[dict[str, Any] | None]  # відповідь OCSP/CRL, ланцюжок
    validated_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))

    __table_args__ = (
        UniqueConstraint(
            "document_version_id", "signer_id", "approval_step_id",
            postgresql_nulls_not_distinct=True,
        ),
        UniqueConstraint("storage_bucket", "storage_key"),
        CheckConstraint(f"signed_content_sha256 {SHA256_HEX_CHECK}", name="sha256_format"),
        CheckConstraint("cert_valid_to > cert_valid_from", name="cert_period"),
        CheckConstraint(
            "signed_at >= cert_valid_from AND signed_at <= cert_valid_to",
            name="signed_within_cert_period",
        ),
        CheckConstraint(
            "validation_status = 'pending' OR validated_at IS NOT NULL", name="validated_has_time"
        ),
        Index("ix_signatures_cert_serial", "cert_issuer", "cert_serial"),
    )
