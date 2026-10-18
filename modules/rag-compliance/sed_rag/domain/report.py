"""Звіт про комплаєнс: що повертає API і що зберігається в `compliance_*`.

Кожне зауваження прив'язане до конкретного пункту акта (`section_ref`) з дослівною цитатою,
перевіреною на входження у фрагмент корпусу (`quote_verified`), і до фрагмента проєкту
(`char_start`/`char_end` у тексті проєкту). Впевненість < 0.85 → `requires_human_review`.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from sed_db.base import CONFIDENCE_THRESHOLD
from sed_db.enums import (
    ComplianceVerdict,
    FindingCategory,
    FindingSeverity,
    RegulatoryLevel,
    RegulatoryStatus,
)

from .models import Coverage, ProvisionKind

VERDICT_LABELS_UK: dict[ComplianceVerdict, str] = {
    ComplianceVerdict.COMPLIANT: "Суперечностей не виявлено",
    ComplianceVerdict.ISSUES_FOUND: "Є зауваження",
    ComplianceVerdict.NON_COMPLIANT: "Виявлено суперечності чинним нормам",
    ComplianceVerdict.INCONCLUSIVE: "Перевірку не завершено: потрібна ручна перевірка",
}

SEVERITY_LABELS_UK: dict[FindingSeverity, str] = {
    FindingSeverity.INFO: "до відома",
    FindingSeverity.MINOR: "незначне",
    FindingSeverity.MAJOR: "суттєве",
    FindingSeverity.CRITICAL: "критичне",
}

CATEGORY_LABELS_UK: dict[FindingCategory, str] = {
    FindingCategory.CONTRADICTION: "Суперечність нормі",
    FindingCategory.OUTDATED_REFERENCE: "Посилання на нечинний акт",
    FindingCategory.MISSING_REQUISITE: "Відсутній реквізит",
    FindingCategory.DSTU_FORMATTING: "Оформлення (ДСТУ 4163:2020)",
    FindingCategory.COMPETENCE_VIOLATION: "Перевищення повноважень",
    FindingCategory.DEADLINE_VIOLATION: "Порушення строків",
    FindingCategory.PERSONAL_DATA: "Персональні дані",
    FindingCategory.OTHER: "Інше",
}


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True)


class Citation(_Out):
    rank: int = Field(ge=1)
    chunk_id: uuid.UUID = Field(description="regulatory_chunks.id = point id у Qdrant")
    source_id: uuid.UUID
    source_title: str
    source_short_title: str | None
    doc_number: str | None
    issuer: str
    level: RegulatoryLevel
    source_status: RegulatoryStatus = Field(description="Статус акта на момент перевірки")
    section_ref: str = Field(examples=["розд. 7, п. 7.1.3"])
    citation_label: str = Field(examples=["Статут ПУЕТ, п. 7.1.3"])
    quoted_text: str = Field(description="Дослівна цитата з фрагмента акта")
    quote_verified: bool = Field(description="Цитату буквально знайдено у фрагменті корпусу")
    is_sample: bool = Field(False, description="Демонстраційний текст акта, не офіційна редакція")
    dense_score: float | None
    sparse_score: float | None
    fused_score: float | None
    rerank_score: float | None


class DraftFragment(_Out):
    provision_id: str
    provision_label: str = Field(examples=["п. 2 розпорядчої частини"])
    text: str
    char_start: int | None = Field(description="Зміщення в тексті проєкту; None — цитату не знайдено дослівно")
    char_end: int | None


class Finding(_Out):
    ordinal: int = Field(ge=1)
    severity: FindingSeverity
    severity_label: str
    category: FindingCategory
    category_label: str
    title: str
    explanation: str
    recommendation: str | None
    fragment: DraftFragment
    confidence: Decimal
    requires_human_review: bool
    issues: list[str] = Field(default_factory=list, description="Чому впевненість знижено")
    verifier: dict[str, Any] | None = Field(None, description="Висновок незалежної повторної перевірки")
    origin: str = Field(description="llm — висновок моделі; reference_check — детермінована перевірка посилань")
    citations: list[Citation] = Field(min_length=1)


class ViolatedClause(_Out):
    """Пункт акта, який може бути порушено (агрегат по зауваженнях)."""

    citation_label: str
    source_title: str
    section_ref: str
    chunk_id: uuid.UUID
    quoted_text: str
    finding_ordinals: list[int]
    max_severity: FindingSeverity
    max_confidence: Decimal


class ProvisionCoverage(_Out):
    provision_id: str
    kind: ProvisionKind
    label: str
    coverage: Coverage
    norms_considered: list[str] = Field(description="Підписи норм, які бачила модель")
    assessed: bool
    no_issue_confidence: Decimal | None = Field(
        None, description="Впевненість, що пункт не суперечить знайденим нормам (якщо зауважень немає)")
    query_rewritten: bool = False


class ComplianceReport(_Out):
    verdict: ComplianceVerdict
    verdict_label: str
    summary: str
    confidence: Decimal
    confidence_threshold: Decimal = CONFIDENCE_THRESHOLD
    requires_human_review: bool
    review_reasons: list[str]
    regulations_as_of: date
    findings: list[Finding]
    violated_clauses: list[ViolatedClause] = Field(
        description="Перелік конкретних пунктів актів, які можуть бути порушені")
    provisions: list[ProvisionCoverage]
    sources_considered: int
    retrieval_config: dict[str, Any]
    model: dict[str, str]
    warnings: list[str] = Field(default_factory=list)
    disclaimer: str = (
        "Звіт сформовано ШІ-асистентом і він не має юридичної сили. Остаточне рішення щодо "
        "відповідності приймає відповідальна особа під час погодження і накладання КЕП."
    )
    # Заповнюється, якщо перевірка збережена для версії документа.
    ai_run_id: uuid.UUID | None = None
    check_id: uuid.UUID | None = None
    document_version_id: uuid.UUID | None = None
