"""Схема structured output для локальної LLM.

Це «сира» відповідь моделі: значення як у тексті (дата рядком, номер з можливим «№»),
плюс дослівна цитата-підстава і самооцінка. Нормалізацію та остаточний confidence
рахує детермінований код (`pipeline/scoring.py`), а не модель.

Усі поля обов'язкові (nullable через `null`), `additionalProperties: false` — сумісно
з `response_format: json_schema, strict: true` у vLLM (xgrammar/outlines) та Ollama.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .requisites import DocumentKind, PartyRole

_EVIDENCE = "Дослівна цитата з тексту документа (до 300 символів), де знайдено значення; null, якщо значення немає"
_CONF = "Самооцінка впевненості 0…1"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LlmKindField(_Strict):
    value: DocumentKind = Field(description="Вид документа")
    evidence: str | None = Field(description=_EVIDENCE, max_length=300)
    confidence: float = Field(ge=0, le=1, description=_CONF)


class LlmTextField(_Strict):
    value: str | None = Field(description="Значення так, як воно записане в документі; null, якщо реквізиту немає",
                              max_length=1024)
    evidence: str | None = Field(description=_EVIDENCE, max_length=300)
    confidence: float = Field(ge=0, le=1, description=_CONF)


class LlmPerson(_Strict):
    full_name: str | None = Field(description="ПІБ або мітка на кшталт [ОСОБА_1] без змін", max_length=256)
    position: str | None = Field(description="Посада", max_length=512)
    org_unit: str | None = Field(description="Підрозділ / організація", max_length=512)
    evidence: str | None = Field(description=_EVIDENCE, max_length=300)


class LlmAuthorField(_Strict):
    value: LlmPerson | None = Field(description="Автор: підписант наказу/листа або заявник/автор записки")
    confidence: float = Field(ge=0, le=1, description=_CONF)


class LlmParty(LlmPerson):
    role: PartyRole


class LlmPartiesField(_Strict):
    value: list[LlmParty] = Field(max_length=50)
    confidence: float = Field(ge=0, le=1, description=_CONF)


class LlmSummaryField(_Strict):
    value: str | None = Field(description="Короткий зміст 1–2 реченнями, до 300 символів", max_length=600)
    confidence: float = Field(ge=0, le=1, description=_CONF)


class LlmExtraction(_Strict):
    document_type: LlmKindField
    document_date: LlmTextField
    registration_number: LlmTextField
    author: LlmAuthorField
    title: LlmTextField
    summary: LlmSummaryField
    interested_parties: LlmPartiesField


def llm_json_schema() -> dict[str, Any]:
    return LlmExtraction.model_json_schema()
