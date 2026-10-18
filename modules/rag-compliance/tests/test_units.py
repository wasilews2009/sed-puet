"""Розбір проєкту, посилання на акти, звірка цитат, скоринг, схеми LLM."""

from __future__ import annotations

import uuid
from datetime import date

from sed_db.enums import FindingCategory, FindingSeverity, RegulatoryLevel, RegulatoryStatus
from sed_ingest.pipeline.pii import RegexPseudonymizer
from sed_rag.audit.draft import parse_draft
from sed_rag.audit.llm_schema import LlmAssessment, schema_of
from sed_rag.audit.references import extract_references, is_outdated, match_reference
from sed_rag.audit.scoring import q4, score_finding, score_no_issue
from sed_rag.audit.verification import QuoteMatch, locate
from sed_rag.domain.models import ActMeta, CandidateFinding, Coverage, ProvisionKind, RetrievedClause
from sed_rag.ports import IndexedSource

from .conftest import CLEAN_ORDER, ORDER_TEXT, STYPENDII


def _masked(text: str) -> str:
    return RegexPseudonymizer().mask(text).masked_text


# --- розбір проєкту -----------------------------------------------------------------

def test_parse_order_structure() -> None:
    d = parse_draft(_masked(ORDER_TEXT))
    assert d.structured
    assert d.context.kind_label == "наказ"
    assert d.context.title == "Про призначення завідувача кафедри економіки"
    assert d.context.signer_position == "Перший проректор"
    kinds = [(p.kind, p.label) for p in d.provisions]
    assert kinds == [(ProvisionKind.PREAMBLE, "констатувальна частина"),
                     (ProvisionKind.ITEM, "п. 1 розпорядчої частини"),
                     (ProvisionKind.ITEM, "п. 2 розпорядчої частини"),
                     (ProvisionKind.ITEM, "п. 3 розпорядчої частини")]
    assert "[ОСОБА_" in d.provisions[1].text and "Петренко" not in d.provisions[1].text
    assert d.context.preamble and d.context.preamble.startswith("Відповідно до Статуту")


def test_parse_subitems_basis_and_tail() -> None:
    text = """РОЗПОРЯДЖЕННЯ
Про організацію практики
НАКАЗУЮ:
1. Організувати практику.
1.1. Затвердити графік.
1.2. Призначити керівників
практики від кафедр.
2. Деканам забезпечити облік.
Підстава: службова записка директора інституту.
Проректор з навчальної роботи [ОСОБА_1]
ПОГОДЖЕНО:
Юрисконсульт [ОСОБА_2]"""
    d = parse_draft(text)
    assert d.context.kind_label == "розпорядження"
    assert d.context.signer_position == "Проректор з навчальної роботи"
    labels = [p.label for p in d.provisions]
    assert labels == ["п. 1 розпорядчої частини", "п. 2 розпорядчої частини", "підстава"]
    assert "практики від кафедр" in d.provisions[0].text and "1.2." in d.provisions[0].text
    assert all("Юрисконсульт" not in p.text for p in d.provisions)


def test_parse_long_item_split() -> None:
    long_item = "1. " + " ".join(f"Підпункт {i} з довгим текстом про організацію освітнього процесу." for i in range(40))
    d = parse_draft(f"НАКАЗ\nПро щось\nНАКАЗУЮ:\n{long_item}\nРектор [ОСОБА_1]", max_chars=600)
    assert len(d.provisions) > 1 and d.provisions[1].label.endswith("(частина 2)")


def test_parse_unstructured_document() -> None:
    d = parse_draft("Службова записка\nПро потребу\n\nПрошу виділити кошти на ремонт аудиторії 205.\n\n"
                    "Прошу також погодити закупівлю обладнання для лабораторії.")
    assert not d.structured and d.provisions and d.provisions[0].label == "фрагмент 1"


# --- посилання --------------------------------------------------------------------------

def _src(meta: ActMeta) -> IndexedSource:
    return IndexedSource(uuid.uuid4(), meta, uuid.uuid4(), "п. 1.1", "Текст першого пункту акта.")


def test_reference_extraction_and_matching() -> None:
    text = ("Відповідно до Закону України «Про вищу освіту», Статуту Університету, Положення про стипендіальну "
            "комісію Університету, наказу МОН від 15.07.2019 № 977 та Положення про невідомий підрозділ, з метою")
    refs = extract_references(text)
    titles = [r.title for r in refs]
    assert "Про вищу освіту" in titles
    assert any(t.startswith("Положення про стипендіальну комісію") for t in titles)
    mon = next(r for r in refs if r.number)
    assert mon.number == "977"

    catalog = [_src(STYPENDII), _src(ActMeta(title="Наказ МОН про ліцензування", level=RegulatoryLevel.MINISTRY,
                                             issuer="МОН України", doc_number="977"))]
    as_of = date(2026, 10, 17)
    stp = match_reference(next(r for r in refs if "стипенд" in r.title), catalog, as_of)
    assert stp is not None and stp.score >= 0.85 and is_outdated(stp)
    assert match_reference(mon, catalog, as_of).score >= 0.9            # збіг за номером
    unknown = next(r for r in refs if "невідомий" in r.title)
    assert match_reference(unknown, catalog, as_of) is None


def test_in_force_on_dates() -> None:
    assert STYPENDII.in_force_on(date(2024, 8, 31)) and not STYPENDII.in_force_on(date(2024, 9, 1))
    future = ActMeta(title="x", level=RegulatoryLevel.UNIVERSITY, issuer="y", effective_from=date(2027, 1, 1))
    assert not future.in_force_on(date(2026, 10, 17))
    draft = ActMeta(title="x", level=RegulatoryLevel.UNIVERSITY, issuer="y", status=RegulatoryStatus.DRAFT)
    assert not draft.in_force_on(date(2026, 10, 17))


# --- цитати --------------------------------------------------------------------------------

NORM = ("Завідувач кафедри обирається за конкурсом таємним голосуванням вченою радою Університету строком на "
        "п’ять років з урахуванням пропозицій трудового колективу кафедри. Ректор укладає із завідувачем кафедри контракт.")


def test_locate_exact_ignores_case_quotes_spaces() -> None:
    loc = locate("обирається за конкурсом  таємним голосуванням вченою радою університету строком на п'ять років", NORM)
    assert loc.match is QuoteMatch.EXACT
    assert loc.text in NORM and loc.text.startswith("обирається")       # дослівно з джерела (з ’)


def test_locate_ellipsis_fuzzy_nearest_none() -> None:
    assert locate("Завідувач кафедри обирається … строком на п'ять років", NORM).match is QuoteMatch.EXACT
    fz = locate("Завідувач кафедри обирається за конкурсом вченою радою Університету строком на п'ять років "
                "з урахуванням пропозицій колективу кафедри", NORM)
    assert fz.match is QuoteMatch.FUZZY and fz.text in NORM
    near = locate("Завідувача кафедри призначає ректор наказом на п'ять років", NORM)
    assert near.match is QuoteMatch.NEAREST and near.text in NORM
    assert locate("Студенти мають право на академічну мобільність у межах країни", NORM).match is QuoteMatch.NONE


# --- скоринг ---------------------------------------------------------------------------------

def _clause(**kw) -> RetrievedClause:  # type: ignore[no-untyped-def]
    act = kw.pop("act", ActMeta(title="Статут", level=RegulatoryLevel.UNIVERSITY, issuer="З"))
    base = dict(chunk_id=uuid.uuid4(), source_id=uuid.uuid4(), act=act, section_ref="п. 7.2.13", heading_path=(),
                content=NORM, text_quality=1.0, dense_score=0.8, sparse_score=5.0, fused_score=1.0,
                dense_rank=1, sparse_rank=1)
    base.update(kw)
    return RetrievedClause(**base)  # type: ignore[arg-type]


def _finding(clause: RetrievedClause, **kw) -> CandidateFinding:  # type: ignore[no-untyped-def]
    base = dict(provision_id="P1", category=FindingCategory.CONTRADICTION, severity=FindingSeverity.MAJOR,
                title="t", explanation="e", recommendation=None, draft_quote="q", norm_quote="n", clause=clause,
                llm_confidence=0.99, rank_in_retrieval=1, verifier_verdict="confirmed", verifier_confidence=0.9)
    base.update(kw)
    return CandidateFinding(**base)  # type: ignore[arg-type]


def test_score_caps_llm_and_applies_factors() -> None:
    conf, issues = score_finding(_finding(_clause()), QuoteMatch.EXACT, True)
    assert conf == 0.95 and issues == []
    conf, issues = score_finding(_finding(_clause()), QuoteMatch.NEAREST, False)
    assert round(conf, 4) == round(0.95 * 0.7 * 0.75, 4) and len(issues) == 2
    conf, _ = score_finding(_finding(_clause(), verifier_verdict="refuted", verifier_reason="норма не стосується"),
                            QuoteMatch.EXACT, True)
    assert conf < 0.5
    conf, issues = score_finding(_finding(_clause(text_quality=0.6)), QuoteMatch.EXACT, True)
    assert conf < 0.85 and any("OCR" in i for i in issues)
    amended = ActMeta(title="С", level=RegulatoryLevel.UNIVERSITY, issuer="З", status=RegulatoryStatus.AMENDED)
    conf, issues = score_finding(_finding(_clause(act=amended)), QuoteMatch.EXACT, True)
    assert round(conf, 4) == round(0.95 * 0.95, 4) and "змінами" in issues[0]
    conf, _ = score_finding(_finding(_clause(), verifier_verdict=None), QuoteMatch.EXACT, True)
    assert round(conf, 4) == round(0.95 * 0.9, 4)


def test_no_issue_confidence_depends_on_coverage() -> None:
    assert score_no_issue(0.99, Coverage.STRONG) == 0.95
    assert score_no_issue(0.99, Coverage.WEAK) < 0.85
    assert q4(0.123456) == q4(0.1235)


def test_llm_schema_is_strict() -> None:
    s = schema_of(LlmAssessment)
    issue = s["$defs"]["LlmIssue"]
    assert issue["additionalProperties"] is False and set(issue["required"]) == set(issue["properties"])
    assert s["additionalProperties"] is False


def test_clean_order_parses() -> None:
    d = parse_draft(_masked(CLEAN_ORDER))
    assert d.context.signer_position == "Ректор" and len(d.provisions) == 2
