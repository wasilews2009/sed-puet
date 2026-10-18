"""Схеми structured output для трьох викликів LLM у графі перевірки.

Модель повертає «сирі» висновки з посиланнями на норми за локальними мітками (N1…N6) і дослівними
цитатами. Чи існує цитата насправді, наскільки висновок надійний і який підсумковий confidence —
вирішує детермінований код (`verification.py`, `scoring.py`), а не модель.

Усі поля обов'язкові (nullable через `null`), `additionalProperties: false` — сумісно з
`response_format: json_schema, strict: true` у vLLM (xgrammar/outlines) та Ollama.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LlmCategory(StrEnum):
    CONTRADICTION = "contradiction"
    COMPETENCE_VIOLATION = "competence_violation"
    DEADLINE_VIOLATION = "deadline_violation"
    PERSONAL_DATA = "personal_data"
    OTHER = "other"


class LlmSeverity(StrEnum):
    MINOR = "minor"
    MAJOR = "major"
    CRITICAL = "critical"


class LlmIssue(_Strict):
    norm_id: str = Field(description="Мітка норми зі списку, напр. «N2»", max_length=8)
    category: LlmCategory
    severity: LlmSeverity
    title: str = Field(description="Суть зауваження одним реченням", max_length=300)
    explanation: str = Field(description="Чому пункт проєкту суперечить нормі (2–4 речення)", max_length=1500)
    recommendation: str | None = Field(description="Як виправити пункт проєкту", max_length=800)
    draft_quote: str = Field(description="Дослівна цитата з пункту проєкту (до 300 символів)", max_length=400)
    norm_quote: str = Field(description="Дослівна цитата з тексту норми (до 400 символів)", max_length=500)
    confidence: float = Field(ge=0, le=1, description="Самооцінка впевненості 0…1")


class LlmAssessment(_Strict):
    applicable_norm_ids: list[str] = Field(description="Мітки норм, що справді стосуються пункту", max_length=12)
    issues: list[LlmIssue] = Field(description="Суперечності; порожній список, якщо їх немає", max_length=6)
    no_issue_confidence: float = Field(
        ge=0, le=1, description="Наскільки впевнено пункт НЕ суперечить жодній з наданих норм, крім наведених issues")


class LlmVerdict(StrEnum):
    CONFIRMED = "confirmed"
    REFUTED = "refuted"
    UNCERTAIN = "uncertain"


class LlmVerification(_Strict):
    verdict: LlmVerdict
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(description="Коротке обґрунтування (1–2 речення)", max_length=600)


class LlmQuery(_Strict):
    provision_id: str = Field(max_length=8)
    query: str = Field(description="Пошуковий запит мовою нормативних актів (5–25 слів)", max_length=300)


class LlmQueryRewrite(_Strict):
    queries: list[LlmQuery] = Field(max_length=40)


def schema_of(model: type[BaseModel]) -> dict[str, Any]:
    return model.model_json_schema()
