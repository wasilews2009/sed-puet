"""Спільні типи Pydantic v2 для API СЕД."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from ..base import CONFIDENCE_THRESHOLD

T = TypeVar("T")

Confidence = Annotated[
    Decimal,
    Field(ge=0, le=1, max_digits=5, decimal_places=4, description="Оцінка впевненості моделі, 0…1"),
]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def needs_human_review(confidence: Decimal | None, threshold: Decimal = CONFIDENCE_THRESHOLD) -> bool:
    """Правило проєкту: score < 0.85 → ручна валідація оператором."""
    return confidence is not None and confidence < threshold


class InputModel(BaseModel):
    """Вхідні DTO: зайві поля — помилка (захист від mass assignment)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ReadModel(BaseModel):
    """Вихідні DTO, що будуються з ORM-об'єктів."""

    model_config = ConfigDict(from_attributes=True)


class Page(ReadModel, Generic[T]):
    items: list[T]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=500)
    offset: int = Field(ge=0)
