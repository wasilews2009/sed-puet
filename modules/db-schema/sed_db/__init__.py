"""Шар даних СЕД ПУЕТ: моделі SQLAlchemy 2.0 + серверні інваріанти PostgreSQL."""

from .base import Base
from . import models  # noqa: F401  (реєстрація всіх таблиць у Base.metadata)

__all__ = ["Base", "models"]
