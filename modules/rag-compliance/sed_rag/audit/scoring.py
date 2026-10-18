"""Підсумкова впевненість зауважень і висновку «суперечностей немає».

Самооцінка моделі — лише верхня межа (≤ 0.95). Кожне зауваження:

    conf = min(llm, 0.95) × цитата_норми × цитата_проєкту × верифікатор × пошук × джерело

| Сигнал | Значення |
|---|---|
| цитата норми | дослівно 1.0 · незначно перефразовано 0.9 · підставлено найближче речення 0.7 · не знайдено 0.4 |
| цитата проєкту | знайдено в тексті 1.0 · ні 0.75 |
| верифікатор | confirmed: 1.0 (≥ 0.8) / 0.9 · uncertain 0.75 · refuted 0.45 · не запускався 0.9 |
| пошук | норма в топ-3 для пункту 1.0 · нижче 0.95 |
| джерело | чинний 1.0 · зі змінами 0.95 · OCR-текст норми з якістю q < 0.9: × (0.7 + 0.3q) · демо-текст ×0.9 |

«Суперечностей немає» для пункту: min(no_issue, 0.95) × покриття (сильне 1.0, слабке 0.8, немає 0.5).
Підсумок перевірки — мінімум по зауваженнях і пунктах: висновок настільки надійний,
наскільки надійна його найслабша ланка.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from sed_db.enums import RegulatoryStatus

from ..domain.models import CandidateFinding, Coverage, RetrievedClause
from .verification import QuoteMatch

LLM_CAP = 0.95
_NORM_QUOTE = {QuoteMatch.EXACT: 1.0, QuoteMatch.FUZZY: 0.9, QuoteMatch.NEAREST: 0.7, QuoteMatch.NONE: 0.4}
_COVERAGE = {Coverage.STRONG: 1.0, Coverage.WEAK: 0.8, Coverage.NONE: 0.5}


def q4(x: float) -> Decimal:
    return Decimal(str(max(0.0, min(1.0, x)))).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def source_factor(c: RetrievedClause) -> tuple[float, list[str]]:
    f, issues = 1.0, []
    if c.act.status is RegulatoryStatus.AMENDED:
        f *= 0.95
        issues.append("акт діє зі змінами: звірте редакцію пункту")
    if c.text_quality < 0.9:
        f *= 0.7 + 0.3 * c.text_quality
        issues.append(f"текст норми розпізнано OCR (якість {c.text_quality:.0%}): звірте цитату з оригіналом")
    if c.act.is_sample:
        f *= 0.9
        issues.append("текст акта демонстраційний, не офіційна редакція")
    return f, issues


def score_finding(f: CandidateFinding, norm_match: QuoteMatch, draft_found: bool) -> tuple[float, list[str]]:
    issues: list[str] = []
    conf = min(f.llm_confidence, LLM_CAP)

    nq = _NORM_QUOTE[norm_match]
    conf *= nq
    if norm_match is QuoteMatch.FUZZY:
        issues.append("цитату норми модель навела не дослівно; у звіті — точний текст пункту")
    elif norm_match is QuoteMatch.NEAREST:
        issues.append("цитати моделі в нормі немає; підставлено найближче речення пункту")
    elif norm_match is QuoteMatch.NONE:
        issues.append("цитату моделі в нормі не знайдено: можлива галюцинація")

    if not draft_found:
        conf *= 0.75
        issues.append("цитату з проєкту не знайдено дослівно")

    if f.origin == "llm":
        v = f.verifier_verdict
        if v == "confirmed":
            conf *= 1.0 if (f.verifier_confidence or 0) >= 0.8 else 0.9
        elif v == "uncertain":
            conf *= 0.75
            issues.append("повторна перевірка не дала однозначного висновку")
        elif v == "refuted":
            conf *= 0.45
            issues.append("повторна перевірка не підтвердила суперечність" +
                          (f": {f.verifier_reason}" if f.verifier_reason else ""))
        else:
            conf *= 0.9
            issues.append("повторна перевірка не виконувалась")
        if f.rank_in_retrieval > 3:
            conf *= 0.95

    sf, s_issues = source_factor(f.clause)
    conf *= sf
    issues.extend(s_issues)
    return conf, issues


def score_no_issue(no_issue_confidence: float, coverage: Coverage) -> float:
    return min(no_issue_confidence, LLM_CAP) * _COVERAGE[coverage]
