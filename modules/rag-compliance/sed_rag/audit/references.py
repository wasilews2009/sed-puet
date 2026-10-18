"""Детермінована перевірка посилань проєкту на інші акти (без LLM).

Проєкт часто ґрунтується на «Положенні про …», «наказі МОН від … № …», «Законі України «…»».
Якщо акт, на який посилається проєкт, у корпусі є, але на дату перевірки скасований або ще не
набрав чинності — це зауваження `outdated_reference` з цитатою з самого акта і статусом на
момент перевірки. Посилання на акт, якого в корпусі немає, — попередження (не зауваження):
відсутність акта в індексі не доводить його нечинність.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from ..corpus.sparse import tokenize
from ..ports import IndexedSource

_STOP = r"(?=,|;|\.(?:\s|$)|\s\(|\sта\s|\sі\s(?:з|на)\s|\sвід\s|\sзатвердж|\sз\sметою|\sпрої|\n|$)"
_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"Закон\w*\s+України\s+«(?P<title>[^»]{3,200})»"),
    re.compile(r"(?P<title>Статут\w*\s+(?:Полтавського\s+університету[^,;.\n]{0,80}|ПУЕТ|університету))"),
    re.compile(r"(?P<title>(?:Положенн\w+|Порядк\w+|Інструкці\w+|Правил\w*|Регламент\w*)\s+(?:про|щодо)\s+.{3,160}?)" + _STOP),
    re.compile(r"(?P<title>наказ\w*\s+(?:Міністерства\s+освіти\s+і\s+науки\s+України|МОН(?:\s+України)?))"
               r"\s+від\s+(?P<date>\d{1,2}\.\d{1,2}\.\d{4})\s*(?:р\.?\s*)?№\s*(?P<number>[\w/-]+)"),
    re.compile(r"(?P<title>постанов\w*\s+Кабінету\s+Міністрів\s+України)\s+від\s+(?P<date>\d{1,2}\.\d{1,2}\.\d{4})"
               r"\s*(?:р\.?\s*)?№\s*(?P<number>[\w/-]+)"),
)
_NUMBER_NEAR = re.compile(r"№\s*(?P<number>[\w/-]+)")


@dataclass(frozen=True, slots=True)
class DraftReference:
    text: str                  # як записано в проєкті
    title: str
    number: str | None
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class ReferenceMatch:
    ref: DraftReference
    source: IndexedSource
    score: float               # 0…1: точність ототожнення акта
    in_force: bool


def extract_references(text: str) -> list[DraftReference]:
    found: list[DraftReference] = []
    taken: list[tuple[int, int]] = []
    for pat in _PATTERNS:
        for m in pat.finditer(text):
            if any(m.start() < e and s < m.end() for s, e in taken):
                continue
            number = m.groupdict().get("number")
            if number is None:
                near = _NUMBER_NEAR.search(text, m.end(), min(len(text), m.end() + 60))
                number = near.group("number") if near else None
            found.append(DraftReference(m.group(0).strip(), m.group("title").strip(), number, m.start(), m.end()))
            taken.append((m.start(), m.end()))
    found.sort(key=lambda r: r.start)
    return found


def _norm_number(n: str | None) -> str | None:
    return re.sub(r"[\s№]", "", n).casefold() if n else None


def match_reference(ref: DraftReference, catalog: list[IndexedSource], as_of: date) -> ReferenceMatch | None:
    """Ототожнення за номером (якщо є) і за назвою (частка слів посилання, що є в назві акта)."""
    ref_tokens = set(tokenize(ref.title)) - {"положенн", "положен", "порядк", "наказ", "закон", "україн"}
    best: ReferenceMatch | None = None
    for src in catalog:
        title_tokens = set(tokenize(" ".join(filter(None, (src.meta.title, src.meta.short_title)))))
        if not ref_tokens or not title_tokens:
            continue
        overlap = len(ref_tokens & title_tokens) / len(ref_tokens)
        number_ok = (ref.number is not None and src.meta.doc_number is not None
                     and _norm_number(ref.number) == _norm_number(src.meta.doc_number))
        if number_ok:
            score = 0.97 if overlap >= 0.3 else 0.9
        elif ref.number is not None and src.meta.doc_number is not None:
            score = overlap * 0.6           # номери різні — це, ймовірно, інший акт
        else:
            score = overlap * 0.95
        if score >= 0.6 and (best is None or score > best.score):
            best = ReferenceMatch(ref, src, round(score, 4), src.meta.in_force_on(as_of))
    return best


def is_outdated(m: ReferenceMatch) -> bool:
    """Скасований, ще не чинний або лише проєкт акта на дату перевірки."""
    return not m.in_force
