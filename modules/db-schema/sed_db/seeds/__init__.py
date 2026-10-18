"""Довідкові дані, які накочує міграція 0002: службовий користувач, види документів
з JSON Schema реквізитів (ПД позначено `"x-pii": true`) і промпт конвеєра завантаження.

Дані тут, а не в тексті міграції, щоб тести й застосунки посилалися на ті самі константи.
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Any

from ..enums import AccessLevel, AiTaskType

# Службовий обліковий запис «Система СЕД»: автор seed-записів. Неактивний, тож не може
# увійти, отримати крок маршруту чи бути підписантом.
SYSTEM_USER_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
SYSTEM_USER_KEYCLOAK_SUB = uuid.UUID("00000000-0000-4000-8000-0000000000aa")

UNSORTED_TYPE_CODE = "UNSORTED"

# Ключі реквізитів з персональними даними (збігаються з sed_ingest PII_FIELD_KEYS).
PII_FIELD_KEYS: frozenset[str] = frozenset({"author", "interested_parties"})

_PERSON: dict[str, Any] = {
    "type": "object",
    "properties": {
        "full_name": {"type": ["string", "null"], "description": "ПІБ / ім'я та прізвище"},
        "position": {"type": ["string", "null"], "description": "Посада"},
        "org_unit": {"type": ["string", "null"], "description": "Підрозділ або організація"},
    },
}


def requisites_schema(required: tuple[str, ...]) -> dict[str, Any]:
    """JSON Schema реквізитів за ДСТУ 4163:2020, спільна для всіх видів."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "document_type": {"type": "string", "description": "Назва виду документа"},
            "document_date": {"type": "string", "format": "date", "description": "Дата документа"},
            "registration_number": {"type": "string", "description": "Реєстраційний індекс"},
            "author": {**_PERSON, "x-pii": True, "description": "Автор / підписант"},
            "title": {"type": "string", "description": "Заголовок до тексту"},
            "summary": {"type": "string", "description": "Короткий зміст"},
            "interested_parties": {
                "type": "array",
                "x-pii": True,
                "description": "Зацікавлені особи: адресати, виконавці, особи, щодо яких видано документ",
                "items": {
                    **_PERSON,
                    "properties": {**_PERSON["properties"], "role": {"type": "string"}},
                },
            },
        },
        "required": list(required),
    }


_ORDER_LIKE = ("document_type", "document_date", "registration_number", "author", "title")
_NOTE_LIKE = ("document_type", "document_date", "author", "title")

# code, назва, гриф за замовчуванням, обов'язкові реквізити
DOCUMENT_TYPES: tuple[tuple[str, str, AccessLevel, tuple[str, ...]], ...] = (
    ("ORDER_MAIN", "Наказ з основної діяльності", AccessLevel.INTERNAL, _ORDER_LIKE),
    ("ORDER_PERSONNEL", "Наказ з кадрових питань (особового складу)", AccessLevel.PERSONAL_DATA, _ORDER_LIKE),
    ("ORDER_STUDENTS", "Наказ щодо контингенту здобувачів освіти", AccessLevel.PERSONAL_DATA, _ORDER_LIKE),
    ("DIRECTIVE", "Розпорядження", AccessLevel.INTERNAL, _ORDER_LIKE),
    ("APPLICATION", "Заява", AccessLevel.PERSONAL_DATA, ("document_type", "document_date", "author")),
    ("MEMO", "Службова записка", AccessLevel.INTERNAL, _NOTE_LIKE),
    ("REPORT_MEMO", "Доповідна записка", AccessLevel.INTERNAL, _NOTE_LIKE),
    ("EXPLANATORY_NOTE", "Пояснювальна записка", AccessLevel.INTERNAL, _NOTE_LIKE),
    ("LETTER", "Службовий лист", AccessLevel.INTERNAL, _ORDER_LIKE),
    ("PROTOCOL", "Протокол (витяг з протоколу)", AccessLevel.INTERNAL, _ORDER_LIKE),
    ("REGULATION", "Положення, інструкція, порядок", AccessLevel.INTERNAL, ("document_type", "title")),
    # Вид за замовчуванням для документів, які не вдалося класифікувати впевнено. Гриф
    # «обмежений», бо вміст ще ніхто не бачив; перед погодженням вид треба уточнити.
    (UNSORTED_TYPE_CODE, "Нерозібрані (вид не визначено)", AccessLevel.RESTRICTED, ("document_type",)),
)

_SEEDS_DIR = Path(__file__).resolve().parent

# Промпти фіксуються разом з хешем: змінити текст = додати нову версію, а не правити файл.
PROMPTS: tuple[tuple[str, int, AiTaskType, str, str], ...] = (
    (
        "ingest.requisites_dstu4163", 1, AiTaskType.NER_EXTRACTION,
        "ingest.requisites_dstu4163.v1.txt",
        "df6476a8a7b53ebf26644e7418d6cd8c8629a2ed79e1c67de17fa54a5acac1b2",
    ),
)


def load_prompt(file_name: str, expected_sha256: str) -> str:
    body = (_SEEDS_DIR / file_name).read_text(encoding="utf-8")
    actual = hashlib.sha256(body.encode()).hexdigest()
    if actual != expected_sha256:
        raise RuntimeError(f"Промпт {file_name} змінено (sha256 {actual}); додайте нову версію")
    return body
