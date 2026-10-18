"""Перетворення сирої відповіді LLM на валідовані реквізити з підсумковим confidence.

Самооцінці моделі не довіряємо: вона лише верхня межа (≤ 0.97). Підсумкова впевненість
реквізиту множиться на незалежні детерміновані сигнали:

    conf = min(llm, 0.97) × ґрунтування × узгодженість × формат × (0.6 + 0.4 × якість_OCR)

* ґрунтування — чи знайдено цитату-підставу в тексті (дослівно або за словесними триграмами,
  щоб пережити дрібні помилки OCR); вигадана цитата різко знижує впевненість;
* узгодженість — чи міститься саме значення в цитаті (номер, ПІБ-мітка, дата);
* формат — дата розпізнається і правдоподібна, номер схожий на реєстраційний індекс;
* якість OCR — 1.0 для текстового шару PDF, середня впевненість Tesseract для скану.

Відсутній обов'язковий реквізит дає 0 → ручна валідація (правило проєкту: < 0.85).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from ..domain.llm_schema import LlmExtraction, LlmPerson
from ..domain.models import OcrResult
from ..domain.requisites import (
    KIND_KEYWORDS,
    REQUIRED_FIELDS,
    DocumentKind,
    DocumentRequisites,
    FieldResult,
    InterestedParty,
    Person,
)
from .normalize import clean_text, fold, normalize_reg_number, parse_ua_date

LLM_SELF_CAP = 0.97
EARLIEST_DATE = date(1991, 8, 24)
PAGE_SEPARATOR = "\n\f\n"

FIELD_LABELS_UK: dict[str, str] = {
    "document_type": "вид документа",
    "document_date": "дата документа",
    "registration_number": "реєстраційний індекс",
    "author": "автор / підписант",
    "title": "заголовок до тексту",
    "summary": "короткий зміст",
    "interested_parties": "зацікавлені особи",
}


@dataclass(frozen=True, slots=True)
class Grounding:
    score: float
    page_no: int | None


class EvidenceIndex:
    """Пошук цитат у замаскованому тексті (саме його бачила модель) з прив'язкою до сторінки."""

    def __init__(self, masked_text: str, ocr: OcrResult) -> None:
        self.ocr = ocr
        parts = masked_text.split(PAGE_SEPARATOR)
        if len(parts) == len(ocr.pages):
            self.pages = [(p.page_no, fold(t)) for p, t in zip(ocr.pages, parts, strict=True)]
        else:  # мітка ПД «перетнула» межу сторінки — без номерів сторінок
            self.pages = [(None, fold(masked_text))]  # type: ignore[list-item]
        self.full = " ".join(t for _, t in self.pages)

    def ground(self, evidence: str | None) -> Grounding:
        if not evidence or len(fold(evidence)) < 3:
            return Grounding(0.0, None)
        fe = fold(evidence)
        for page_no, text in self.pages:
            if fe in text:
                return Grounding(1.0, page_no)
        words = fe.split()
        grams = [" ".join(words[i:i + 3]) for i in range(len(words) - 2)] if len(words) >= 3 else words
        best = Grounding(0.0, None)
        for page_no, text in self.pages:
            hit = sum(1 for g in grams if g in text) / len(grams)
            if hit > best.score:
                best = Grounding(hit, page_no)
        return best

    def contains(self, value: str) -> bool:
        return fold(value) in self.full

    def header(self, chars: int = 1500) -> str:
        return self.full[:chars]

    def quality(self, page_no: int | None, value: str | None = None) -> float:
        return self.ocr.quality_of_page(page_no, value)


def _ocr_factor(q: float) -> float:
    return 0.6 + 0.4 * max(0.0, min(1.0, q))


class _Scorer:
    def __init__(self, idx: EvidenceIndex, required: frozenset[str], restore: Callable[[str], str]) -> None:
        self.idx = idx
        self.required = required
        self.restore = restore

    def absent(self, key: str, llm_conf: float) -> FieldResult[Any]:
        if key in self.required:
            return FieldResult(value=None, confidence=0,
                               issues=[f"Обов'язковий реквізит «{FIELD_LABELS_UK[key]}» не знайдено"])
        return FieldResult(value=None, confidence=min(llm_conf, LLM_SELF_CAP) * _ocr_factor(self.idx.ocr.quality))

    def grounded(
        self, llm_conf: float, evidence: str | None, issues: list[str], *,
        value: str | None = None, consistent: bool = True, format_ok: bool = True,
    ) -> tuple[float, int | None]:
        g = self.idx.ground(evidence)
        c = min(llm_conf, LLM_SELF_CAP)
        if g.score < 0.95:
            issues.append("Цитату-підставу не знайдено в тексті дослівно" if g.score >= 0.6
                          else "Значення не підтверджується текстом документа (можлива галюцинація)")
            c *= g.score if g.score >= 0.6 else g.score * 0.5
        if not consistent:
            issues.append("Значення не збігається з цитатою-підставою")
            c *= 0.8
        if not format_ok:
            c *= 0.6
        q = self.idx.quality(g.page_no, value)
        if q < 0.75:
            issues.append(f"Низька якість розпізнавання ({q:.0%})")
        return c * _ocr_factor(q), g.page_no

    def evidence(self, e: str | None) -> str | None:
        return self.restore(e) if e else None


def _has_word(text: str, word: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text) is not None


def _contains(container: str | None, value: str) -> bool:
    return bool(container) and fold(value) in fold(container)  # type: ignore[arg-type]


def score_extraction(
    raw: LlmExtraction,
    masked_text: str,
    ocr: OcrResult,
    restore: Callable[[str], str],
    today: date,
) -> DocumentRequisites:
    idx = EvidenceIndex(masked_text, ocr)

    # --- Вид документа (визначає набір обов'язкових реквізитів) -------------------
    kind = raw.document_type.value
    required = REQUIRED_FIELDS[kind]
    s = _Scorer(idx, required, restore)
    kind_issues: list[str] = []
    header = idx.header()
    own = KIND_KEYWORDS[kind]
    others = {k for k, kws in KIND_KEYWORDS.items()
              if k is not kind and kws and set(kws) != set(own) and _has_word(header, kws[0])}
    if own and any(_has_word(header, kw) for kw in own):
        kw_factor = 1.0
    elif others:
        kw_factor = 0.6
        kind_issues.append("У шапці документа вказано інший вид документа")
    else:
        kw_factor = 0.85
        kind_issues.append("Назву виду документа в тексті не знайдено")
    first_q = ocr.pages[0].quality if ocr.pages else 0.0
    document_type = FieldResult[DocumentKind](
        value=kind,
        confidence=min(raw.document_type.confidence, LLM_SELF_CAP) * kw_factor * _ocr_factor(first_q),
        evidence=s.evidence(raw.document_type.evidence),
        page_no=1 if ocr.pages else None,
        issues=kind_issues,
    )

    # --- Дата ---------------------------------------------------------------------
    rd = raw.document_date
    if rd.value is None:
        document_date: FieldResult[date] = s.absent("document_date", rd.confidence)
    else:
        issues: list[str] = []
        parsed = parse_ua_date(rd.value)
        fmt_ok = parsed is not None and EARLIEST_DATE <= parsed <= today + timedelta(days=1)
        if parsed is None:
            issues.append(f"Не вдалося розпізнати дату «{rd.value}»")
        elif not fmt_ok:
            issues.append("Дата неправдоподібна (у майбутньому або до 24.08.1991)")
        consistent = parsed is not None and parse_ua_date(rd.evidence) == parsed
        conf, page = s.grounded(rd.confidence, rd.evidence, issues, value=rd.value,
                                consistent=consistent, format_ok=fmt_ok)
        document_date = FieldResult[date](
            value=parsed if fmt_ok else None, confidence=conf if fmt_ok else min(conf, 0.3),
            evidence=s.evidence(rd.evidence), page_no=page, issues=issues)

    # --- Реєстраційний індекс -----------------------------------------------------
    rn = raw.registration_number
    if rn.value is None:
        registration_number: FieldResult[str] = s.absent("registration_number", rn.confidence)
    else:
        issues = []
        number = normalize_reg_number(rn.value)
        if number is None:
            issues.append(f"«{rn.value}» не схоже на реєстраційний індекс")
        consistent = number is not None and _contains(
            re.sub(r"\s*([/\\-])\s*", r"\1", rn.evidence or ""), number)
        conf, page = s.grounded(rn.confidence, rn.evidence, issues, value=rn.value,
                                consistent=consistent, format_ok=number is not None)
        registration_number = FieldResult[str](
            value=number, confidence=conf if number else min(conf, 0.3),
            evidence=s.evidence(rn.evidence), page_no=page, issues=issues)

    # --- Автор --------------------------------------------------------------------
    ra = raw.author
    if ra.value is None or not (ra.value.full_name or ra.value.position):
        author: FieldResult[Person] = s.absent("author", ra.confidence)
    else:
        issues = []
        conf, page, person = _score_person(s, ra.value, ra.confidence, issues)
        author = FieldResult[Person](value=person, confidence=conf, evidence=s.evidence(ra.value.evidence),
                                     page_no=page, issues=issues)

    # --- Заголовок до тексту ------------------------------------------------------
    rt = raw.title
    title_value = clean_text(rt.value, 1024)
    if title_value is None:
        title: FieldResult[str] = s.absent("title", rt.confidence)
    else:
        issues = []
        conf, page = s.grounded(rt.confidence, rt.evidence, issues,
                                consistent=_contains(rt.evidence, title_value[:60]))
        title = FieldResult[str](value=restore(title_value), confidence=conf, evidence=s.evidence(rt.evidence),
                                 page_no=page, issues=issues)

    # --- Короткий зміст (генеративний: перевіряємо лексичну опору на текст) --------
    rs = raw.summary
    summary_value = clean_text(rs.value, 600)
    if summary_value is None:
        summary: FieldResult[str] = s.absent("summary", rs.confidence)
    else:
        issues = []
        words = [w for w in re.findall(r"[^\W\d_]{5,}", fold(summary_value))]
        support = (sum(1 for w in words if w[:5] in idx.full) / len(words)) if words else 0.0
        if support < 0.5:
            issues.append("Короткий зміст слабко підтверджується текстом документа")
        conf = min(rs.confidence, LLM_SELF_CAP) * (0.7 + 0.3 * support) * _ocr_factor(ocr.quality)
        summary = FieldResult[str](value=restore(summary_value), confidence=conf, issues=issues)

    # --- Зацікавлені особи --------------------------------------------------------
    rp = raw.interested_parties
    parties: list[InterestedParty] = []
    seen: set[tuple[str, str]] = set()
    p_issues: list[str] = []
    p_conf = min(rp.confidence, LLM_SELF_CAP)
    first_page: int | None = None
    for party in rp.value:
        if not (party.full_name or party.position):
            continue
        key = (fold(party.full_name or party.position or ""), party.role.value)
        if key in seen:
            continue
        seen.add(key)
        local: list[str] = []
        conf, page, person = _score_person(s, party, rp.confidence, local)
        p_conf = min(p_conf, conf)
        first_page = first_page or page
        p_issues.extend(f"{person.full_name or person.position}: {msg}" for msg in local)
        parties.append(InterestedParty(**person.model_dump(), role=party.role))
    if parties:
        interested = FieldResult[list[InterestedParty]](value=parties, confidence=p_conf, page_no=first_page,
                                                        issues=p_issues)
    else:
        interested = FieldResult[list[InterestedParty]](
            value=[], confidence=min(rp.confidence, LLM_SELF_CAP) * _ocr_factor(ocr.quality))

    return DocumentRequisites(
        document_type=document_type, document_date=document_date, registration_number=registration_number,
        author=author, title=title, summary=summary, interested_parties=interested,
    )


def _score_person(s: _Scorer, p: LlmPerson, llm_conf: float, issues: list[str]) -> tuple[float, int | None, Person]:
    name = clean_text(p.full_name, 256)
    consistent = True
    if name:
        # Ім'я, якого немає ані в цитаті, ані в тексті, модель могла лише вигадати
        # (справжні ПІБ вона бачить тільки як мітки [ОСОБА_n]).
        consistent = _contains(p.evidence, name) and s.idx.contains(name)
    elif p.position:
        consistent = _contains(p.evidence, p.position.split()[0])
    conf, page = s.grounded(llm_conf, p.evidence, issues, value=name or p.position, consistent=consistent)
    person = Person(
        full_name=s.restore(name) if name else None,
        position=clean_text(s.restore(p.position), 512) if p.position else None,
        org_unit=clean_text(s.restore(p.org_unit), 512) if p.org_unit else None,
    )
    return conf, page, person
