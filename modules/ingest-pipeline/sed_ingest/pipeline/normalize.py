"""Нормалізація значень реквізитів у форматах українського діловодства."""

from __future__ import annotations

import re
from datetime import date

_MONTHS = {
    "січня": 1, "лютого": 2, "березня": 3, "квітня": 4, "травня": 5, "червня": 6,
    "липня": 7, "серпня": 8, "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
    "січень": 1, "лютий": 2, "березень": 3, "квітень": 4, "травень": 5, "червень": 6,
    "липень": 7, "серпень": 8, "вересень": 9, "жовтень": 10, "листопад": 11, "грудень": 12,
}
_NUMERIC = re.compile(r"(?<!\d)(\d{1,2})[./-](\d{1,2})[./-](\d{4}|\d{2})(?!\d)")
_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
# «17» жовтня 2026 р. / 17 жовтня 2026 року
_VERBAL = re.compile(r"[«\"“]?(\d{1,2})[»\"”]?\s+([а-яіїєґ]+)\s+(\d{4})", re.IGNORECASE)


def parse_ua_date(raw: str | None) -> date | None:
    """«17.10.2026», «17.10.26», «2026-10-17», «“17” жовтня 2026 р.» → date."""
    if not raw:
        return None
    s = raw.strip()
    try:
        if m := _ISO.search(s):
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if m := _NUMERIC.search(s):
            d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
            year = int(y) + (2000 if len(y) == 2 else 0)
            return date(year, mo, d)
        if m := _VERBAL.search(s):
            month = _MONTHS.get(m.group(2).lower())
            if month:
                return date(int(m.group(3)), month, int(m.group(1)))
    except ValueError:      # 31.02.2026 тощо
        return None
    return None


_NUMBER_PREFIX = re.compile(r"^\s*(?:№|No\.?|Nr\.?|N(?=[\s\d])|номер)\s*", re.IGNORECASE)
_NUMBER_OK = re.compile(r"^[0-9A-Za-zА-ЯІЇЄҐа-яіїєґ'’/\\\-.]{1,64}$")


def normalize_reg_number(raw: str | None) -> str | None:
    """«№ 245-од» → «245-од», «01-12 / 345» → «01-12/345». None, якщо не схоже на індекс."""
    if not raw:
        return None
    s = _NUMBER_PREFIX.sub("", raw.strip())
    s = re.sub(r"\s*([/\\-])\s*", r"\1", s)
    s = s.strip(" .,;")
    if not s or not any(ch.isdigit() for ch in s) or not _NUMBER_OK.match(s):
        return None
    return s


def clean_text(raw: str | None, max_len: int) -> str | None:
    if raw is None:
        return None
    s = re.sub(r"\s+", " ", raw).strip()
    return s[:max_len] if s else None


_FOLD = str.maketrans({"’": "'", "ʼ": "'", "`": "'", "«": '"', "»": '"', "“": '"', "”": '"', "–": "-", "—": "-"})


def fold(s: str) -> str:
    """Канонічна форма для порівняння цитат: регістр, лапки, апострофи, пробіли."""
    return re.sub(r"\s+", " ", s.translate(_FOLD)).strip().casefold()
