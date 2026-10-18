"""Реквізити документа за ДСТУ 4163:2020, які вилучає пайплайн інжесту.

Відповідність запиту реквізитам стандарту:

| Поле                 | Реквізит ДСТУ 4163:2020                                        |
|----------------------|----------------------------------------------------------------|
| `document_type`      | Назва виду документа                                           |
| `document_date`      | Дата документа                                                 |
| `registration_number`| Реєстраційний індекс документа                                 |
| `author`             | Підпис (посада, ім'я та прізвище підписанта) / заявник          |
| `title`              | Заголовок до тексту                                            |
| `summary`            | Короткий зміст (анотація для картки, генерується ШІ)           |
| `interested_parties` | Адресат, виконавці, особи, яких стосується, відмітка про контроль |
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_DOWN, Decimal
from enum import StrEnum
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from sed_db.base import CONFIDENCE_THRESHOLD
from sed_db.schemas.common import needs_human_review

T = TypeVar("T")


class DocumentKind(StrEnum):
    ORDER_MAIN = "order_main"              # наказ з основної діяльності
    ORDER_PERSONNEL = "order_personnel"    # наказ з кадрових питань (особового складу)
    ORDER_STUDENTS = "order_students"      # наказ щодо контингенту здобувачів освіти
    DIRECTIVE = "directive"                # розпорядження
    APPLICATION = "application"            # заява
    MEMO = "memo"                          # службова записка
    REPORT_MEMO = "report_memo"            # доповідна записка
    EXPLANATORY_NOTE = "explanatory_note"  # пояснювальна записка
    LETTER = "letter"                      # службовий лист
    PROTOCOL = "protocol"                  # протокол (витяг з протоколу)
    REGULATION = "regulation"              # положення, інструкція, порядок
    OTHER = "other"


KIND_LABELS_UK: dict[DocumentKind, str] = {
    DocumentKind.ORDER_MAIN: "Наказ з основної діяльності",
    DocumentKind.ORDER_PERSONNEL: "Наказ з кадрових питань (особового складу)",
    DocumentKind.ORDER_STUDENTS: "Наказ щодо контингенту здобувачів освіти",
    DocumentKind.DIRECTIVE: "Розпорядження",
    DocumentKind.APPLICATION: "Заява",
    DocumentKind.MEMO: "Службова записка",
    DocumentKind.REPORT_MEMO: "Доповідна записка",
    DocumentKind.EXPLANATORY_NOTE: "Пояснювальна записка",
    DocumentKind.LETTER: "Службовий лист",
    DocumentKind.PROTOCOL: "Протокол",
    DocumentKind.REGULATION: "Положення / інструкція / порядок",
    DocumentKind.OTHER: "Інший документ",
}

# Слова, якими вид документа позначено в самому тексті (реквізит «назва виду документа»).
# Незалежний від LLM сигнал для confidence класифікації (пошук цілого слова в шапці).
KIND_KEYWORDS: dict[DocumentKind, tuple[str, ...]] = {
    DocumentKind.ORDER_MAIN: ("наказ",),
    DocumentKind.ORDER_PERSONNEL: ("наказ",),
    DocumentKind.ORDER_STUDENTS: ("наказ",),
    DocumentKind.DIRECTIVE: ("розпорядження",),
    DocumentKind.APPLICATION: ("заява",),
    DocumentKind.MEMO: ("службова записка",),
    DocumentKind.REPORT_MEMO: ("доповідна записка",),
    DocumentKind.EXPLANATORY_NOTE: ("пояснювальна записка",),
    DocumentKind.LETTER: ("лист",),
    DocumentKind.PROTOCOL: ("протокол",),
    DocumentKind.REGULATION: ("положення", "інструкція", "порядок"),
    DocumentKind.OTHER: (),
}

# Обов'язкові реквізити за видом документа (відсутність → confidence 0 → ручна валідація).
# Заява та службові записки отримують реєстраційний індекс лише при реєстрації, тому номер не вимагається.
_ORDER_LIKE = frozenset({"document_type", "document_date", "registration_number", "author", "title"})
REQUIRED_FIELDS: dict[DocumentKind, frozenset[str]] = {
    DocumentKind.ORDER_MAIN: _ORDER_LIKE,
    DocumentKind.ORDER_PERSONNEL: _ORDER_LIKE,
    DocumentKind.ORDER_STUDENTS: _ORDER_LIKE,
    DocumentKind.DIRECTIVE: _ORDER_LIKE,
    DocumentKind.APPLICATION: frozenset({"document_type", "document_date", "author"}),
    DocumentKind.MEMO: frozenset({"document_type", "document_date", "author"}),
    DocumentKind.REPORT_MEMO: frozenset({"document_type", "document_date", "author"}),
    DocumentKind.EXPLANATORY_NOTE: frozenset({"document_type", "document_date", "author"}),
    DocumentKind.LETTER: frozenset({"document_type", "document_date", "registration_number", "author"}),
    DocumentKind.PROTOCOL: frozenset({"document_type", "document_date", "registration_number"}),
    DocumentKind.REGULATION: frozenset({"document_type", "title"}),
    DocumentKind.OTHER: frozenset({"document_type"}),
}

# Ключі реквізитів, що містять персональні дані. Мають бути позначені `"x-pii": true`
# у `document_types.requisites_schema`, щоб тригер audit_logs маскував їх у журналі.
PII_FIELD_KEYS: frozenset[str] = frozenset({"author", "interested_parties"})
FIELD_KEYS: tuple[str, ...] = (
    "document_type", "document_date", "registration_number", "author", "title", "summary",
    "interested_parties",
)


class PartyRole(StrEnum):
    ADDRESSEE = "addressee"        # адресат
    EXECUTOR = "executor"          # виконавець, відповідальний
    SUBJECT = "subject"            # особа, щодо якої видано документ (призначення, відрахування…)
    CONTROLLER = "controller"      # контроль за виконанням покладено на
    APPROVER = "approver"          # погоджує / візує
    ACKNOWLEDGER = "acknowledger"  # ознайомлення
    OTHER = "other"


PARTY_ROLE_LABELS_UK: dict[PartyRole, str] = {
    PartyRole.ADDRESSEE: "Адресат",
    PartyRole.EXECUTOR: "Виконавець",
    PartyRole.SUBJECT: "Особа, якої стосується документ",
    PartyRole.CONTROLLER: "Контроль за виконанням",
    PartyRole.APPROVER: "Погоджувач",
    PartyRole.ACKNOWLEDGER: "Ознайомлення",
    PartyRole.OTHER: "Інше",
}


class Person(BaseModel):
    model_config = ConfigDict(frozen=True)

    full_name: str | None = Field(default=None, description="ПІБ / ім'я та прізвище")
    position: str | None = Field(default=None, description="Посада")
    org_unit: str | None = Field(default=None, description="Підрозділ або організація")


class InterestedParty(Person):
    role: PartyRole


def quantize_confidence(value: float | Decimal) -> Decimal:
    """Приведення до NUMERIC(5,4) БД. Округлення донизу на половині — не завищуємо впевненість."""
    v = min(max(Decimal(str(value)), Decimal(0)), Decimal(1))
    return v.quantize(Decimal("0.0001"), rounding=ROUND_HALF_DOWN)


class FieldResult(BaseModel, Generic[T]):
    """Значення реквізиту з оцінкою впевненості та підставою."""

    value: T | None = None
    confidence: Decimal = Field(ge=0, le=1, description="Підсумкова впевненість, 0…1")
    evidence: str | None = Field(default=None, description="Цитата з тексту, на якій ґрунтується значення")
    page_no: int | None = Field(default=None, ge=1)
    issues: list[str] = Field(default_factory=list, description="Чому впевненість знижено")

    @field_validator("confidence", mode="before")
    @classmethod
    def _q(cls, v: float | Decimal) -> Decimal:
        return quantize_confidence(v)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def requires_human_review(self) -> bool:
        return needs_human_review(self.confidence)


class DocumentRequisites(BaseModel):
    """Валідовані реквізити вхідного документа."""

    document_type: FieldResult[DocumentKind]
    document_date: FieldResult[date]
    registration_number: FieldResult[str]
    author: FieldResult[Person]
    title: FieldResult[str]
    summary: FieldResult[str]
    interested_parties: FieldResult[list[InterestedParty]]

    def fields(self) -> dict[str, FieldResult]:  # type: ignore[type-arg]
        return {k: getattr(self, k) for k in FIELD_KEYS}

    @computed_field  # type: ignore[prop-decorator]
    @property
    def document_type_label(self) -> str | None:
        v = self.document_type.value
        return KIND_LABELS_UK[v] if v is not None else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def confidence(self) -> Decimal:
        """Впевненість документа = мінімум по реквізитах: один слабкий реквізит → ручна валідація."""
        return min(f.confidence for f in self.fields().values())

    @computed_field  # type: ignore[prop-decorator]
    @property
    def requires_human_review(self) -> bool:
        return any(f.requires_human_review for f in self.fields().values())


CONFIDENCE_THRESHOLD_FLOAT = float(CONFIDENCE_THRESHOLD)
