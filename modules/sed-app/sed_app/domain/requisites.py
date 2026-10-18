"""Перевірка значень реквізитів, які оператор вводить або виправляє вручну."""

from __future__ import annotations

from datetime import date
from typing import Any

from .errors import InvalidInput
from .labels import FIELD_LABELS_UK

_PERSON_KEYS = ("full_name", "position", "org_unit")
_PARTY_KEYS = (*_PERSON_KEYS, "role")
_MAX_STR = {"document_type": 256, "registration_number": 64, "title": 1024, "summary": 4000}


def _person(value: Any, keys: tuple[str, ...], where: str) -> dict[str, str | None]:
    if isinstance(value, str):          # оператор ввів лише ПІБ
        value = {"full_name": value}
    if not isinstance(value, dict):
        raise InvalidInput(f"{where}: очікується об'єкт з полями {', '.join(keys)}")
    unknown = set(value) - set(keys)
    if unknown:
        raise InvalidInput(f"{where}: невідомі поля {', '.join(sorted(unknown))}")
    out: dict[str, str | None] = {}
    for k in keys:
        v = value.get(k)
        if v is not None and not isinstance(v, str):
            raise InvalidInput(f"{where}: поле {k} має бути рядком")
        out[k] = (v or "").strip()[:256] or None
    if not out.get("full_name"):
        raise InvalidInput(f"{where}: потрібне ім'я та прізвище")
    return out


def normalize_requisite(key: str, value: Any) -> Any:
    """Повертає значення у форматі `documents.requisites` або кидає `InvalidInput`."""
    if key not in FIELD_LABELS_UK:
        raise InvalidInput(f"Невідомий реквізит «{key}»")
    label = FIELD_LABELS_UK[key]
    if key == "document_date":
        try:
            return date.fromisoformat(str(value).strip()).isoformat()
        except ValueError:
            raise InvalidInput(f"{label}: очікується дата у форматі РРРР-ММ-ДД") from None
    if key == "author":
        return _person(value, _PERSON_KEYS, label)
    if key == "interested_parties":
        if not isinstance(value, list) or len(value) > 200:
            raise InvalidInput(f"{label}: очікується список осіб (до 200)")
        return [_person(v, _PARTY_KEYS, f"{label}, №{i + 1}") for i, v in enumerate(value)]
    if not isinstance(value, str) or not value.strip():
        raise InvalidInput(f"{label}: очікується непорожній текст")
    return " ".join(value.split())[: _MAX_STR[key]]
