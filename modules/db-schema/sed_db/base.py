"""Базові класи ORM: декларативна база, конвенція іменування, міксини."""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Date,
    DateTime,
    Enum as SAEnum,
    MetaData,
    Numeric,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Детерміновані імена обмежень потрібні Alembic, щоб autogenerate/downgrade
# не створював анонімних constraint-ів, які неможливо видалити за назвою.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# Поріг впевненості з правил проєкту: нижче — обов'язкова ручна валідація.
CONFIDENCE_THRESHOLD = Decimal("0.85")

Confidence = Numeric(5, 4)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map = {
        uuid.UUID: UUID(as_uuid=True),
        datetime: DateTime(timezone=True),
        date: Date(),
        dict[str, Any]: JSONB,
        list[Any]: JSONB,
    }


def pg_enum(enum_cls: type[enum.Enum], name: str) -> SAEnum:
    """Нативний ENUM PostgreSQL, що зберігає значення (`value`), а не імена членів."""
    return SAEnum(
        enum_cls,
        name=name,
        values_callable=lambda members: [m.value for m in members],
        validate_strings=True,
    )


class UUIDPkMixin:
    """UUID первинний ключ: генерується і в Python (до flush), і в БД (для сирого SQL)."""

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )
