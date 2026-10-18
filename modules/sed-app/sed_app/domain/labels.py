"""Українські підписи значень довідників для веб-інтерфейсу (доповнюють `sed_db.enums.LABELS_UK`)."""

from __future__ import annotations

from enum import StrEnum

from sed_db.enums import (
    LABELS_UK,
    AccessLevel,
    ComplianceVerdict,
    DocumentDirection,
    DocumentStatus,
    FindingCategory,
    FindingSeverity,
    FindingStatus,
    HumanReviewStatus,
    OrgUnitKind,
    RouteStatus,
    SignatureValidation,
    StepKind,
    StepStatus,
    UserRole,
)

_EXTRA: dict[StrEnum, str] = {
    AccessLevel.PUBLIC: "Публічна інформація",
    AccessLevel.INTERNAL: "Для внутрішнього користування",
    AccessLevel.RESTRICTED: "Обмежений доступ",
    AccessLevel.PERSONAL_DATA: "Містить персональні дані",
    DocumentDirection.INTERNAL: "Внутрішній",
    DocumentDirection.INCOMING: "Вхідний",
    DocumentDirection.OUTGOING: "Вихідний",
    StepStatus.WAITING: "Очікує черги",
    StepStatus.ACTIVE: "На розгляді",
    StepStatus.APPROVED: "Погоджено",
    StepStatus.APPROVED_WITH_REMARKS: "Погоджено із зауваженнями",
    StepStatus.REJECTED: "Відхилено",
    StepStatus.SKIPPED: "Пропущено",
    StepStatus.DELEGATED: "Передано заступнику",
    StepStatus.EXPIRED: "Строк минув",
    RouteStatus.DRAFT: "Проєкт маршруту",
    RouteStatus.ACTIVE: "Триває",
    RouteStatus.COMPLETED: "Завершено",
    RouteStatus.REJECTED: "Відхилено",
    RouteStatus.CANCELLED: "Скасовано",
    HumanReviewStatus.NOT_REQUIRED: "Не потребує перевірки",
    ComplianceVerdict.COMPLIANT: "Суперечностей не виявлено",
    ComplianceVerdict.ISSUES_FOUND: "Є зауваження",
    ComplianceVerdict.NON_COMPLIANT: "Виявлено суперечності чинним нормам",
    ComplianceVerdict.INCONCLUSIVE: "Потрібна ручна перевірка",
    FindingSeverity.INFO: "До відома",
    FindingSeverity.MINOR: "Незначне",
    FindingSeverity.MAJOR: "Суттєве",
    FindingSeverity.CRITICAL: "Критичне",
    FindingCategory.CONTRADICTION: "Суперечність нормі",
    FindingCategory.OUTDATED_REFERENCE: "Посилання на нечинний акт",
    FindingCategory.MISSING_REQUISITE: "Відсутній реквізит",
    FindingCategory.DSTU_FORMATTING: "Оформлення (ДСТУ 4163:2020)",
    FindingCategory.COMPETENCE_VIOLATION: "Перевищення повноважень",
    FindingCategory.DEADLINE_VIOLATION: "Порушення строків",
    FindingCategory.PERSONAL_DATA: "Персональні дані",
    FindingCategory.OTHER: "Інше",
    FindingStatus.OPEN: "Відкрите",
    FindingStatus.ACCEPTED: "Прийнято до виправлення",
    FindingStatus.DISMISSED: "Відхилено як хибне",
    FindingStatus.FIXED: "Виправлено",
    SignatureValidation.PENDING: "Перевіряється",
    SignatureValidation.VALID: "Чинний",
    SignatureValidation.INVALID: "Недійсний",
    SignatureValidation.CERT_REVOKED: "Сертифікат відкликано",
    SignatureValidation.CERT_EXPIRED: "Строк сертифіката минув",
    SignatureValidation.INDETERMINATE: "Не вдалося перевірити",
    OrgUnitKind.UNIVERSITY: "Університет",
    OrgUnitKind.RECTORATE: "Ректорат",
    OrgUnitKind.INSTITUTE: "Навчально-науковий інститут",
    OrgUnitKind.FACULTY: "Факультет",
    OrgUnitKind.DEPARTMENT: "Кафедра",
    OrgUnitKind.DIVISION: "Відділ / управління",
    OrgUnitKind.CENTER: "Центр",
    OrgUnitKind.COLLEGIAL_BODY: "Колегіальний орган",
}

LABELS: dict[StrEnum, str] = {**_EXTRA, **LABELS_UK}

# Реквізити за ДСТУ 4163:2020, які вилучає інжест (ключі з sed_ingest.domain.requisites).
FIELD_LABELS_UK: dict[str, str] = {
    "document_type": "Назва виду документа",
    "document_date": "Дата документа",
    "registration_number": "Реєстраційний індекс",
    "author": "Підпис (автор / підписант)",
    "title": "Заголовок до тексту",
    "summary": "Короткий зміст",
    "interested_parties": "Адресати, виконавці, зацікавлені особи",
}

DICTIONARIES: tuple[type[StrEnum], ...] = (
    AccessLevel, DocumentDirection, DocumentStatus, StepKind, StepStatus, RouteStatus, UserRole,
    HumanReviewStatus, ComplianceVerdict, FindingSeverity, FindingCategory, FindingStatus,
    SignatureValidation, OrgUnitKind,
)


def label(value: StrEnum) -> str:
    return LABELS.get(value, value.value)


def all_labels() -> dict[str, dict[str, str]]:
    """{"DocumentStatus": {"draft": "Проєкт", …}, …} — для веб-інтерфейсу одним запитом."""
    from sed_ingest.domain.requisites import KIND_LABELS_UK   # вид документа, як його класифікує ШІ

    out = {enum.__name__: {m.value: label(m) for m in enum} for enum in DICTIONARIES}
    out["DocumentKind"] = {k.value: v for k, v in KIND_LABELS_UK.items()}
    return out
