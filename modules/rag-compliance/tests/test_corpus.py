"""Очищення тексту, сегментація актів на пункти, BM25-токенізація."""

from __future__ import annotations

from pathlib import Path

import pytest

from sed_rag.corpus.segmentation import clean_text, segment
from sed_rag.corpus.sparse import Bm25Encoder, stem, token_index, tokenize

from .conftest import FIXTURES, ROOT


def _seg(name: str, **kw: int) -> list:  # type: ignore[type-arg]
    return segment(clean_text((FIXTURES / name).read_text(encoding="utf-8")), **kw)


def test_statute_numbering_and_subheadings() -> None:
    clauses = _seg("statut_SAMPLE.txt")
    refs = [c.section_ref for c in clauses]
    assert refs == ["п. 1.1", "п. 1.2", "п. 7.1.1", "п. 7.1.2", "п. 7.1.3", "п. 7.2.1", "п. 7.2.2", "п. 12.1", "п. 12.2"]
    c = clauses[5]
    # «7.2. Керівник кафедри» — підзаголовок, а не окремий фрагмент; він у шляху заголовків.
    assert c.heading_path == ("7. ОРГАНИ УПРАВЛІННЯ УНІВЕРСИТЕТОМ", "7.2. Керівник кафедри")
    assert c.text.startswith("Завідувач кафедри обирається за конкурсом")
    assert "[[page" not in " ".join(x.text for x in clauses)


def test_law_articles_and_parts() -> None:
    clauses = _seg("zakon_vo_SAMPLE.txt")
    assert [c.section_ref for c in clauses] == ["ст. 35, ч. 1", "ст. 35, ч. 2", "ст. 35, ч. 3", "ст. 36, ч. 1", "ст. 36, ч. 2"]
    assert clauses[1].heading_path[-1] == "Стаття 35. Керівник структурного підрозділу закладу вищої освіти"
    assert clauses[0].heading_path[0].startswith("Розділ V")


def test_roman_sections() -> None:
    clauses = _seg("mon_akadem_SAMPLE.txt")
    assert [c.section_ref for c in clauses] == ["розд. I, п. 1", "розд. I, п. 2", "розд. II, п. 1"]
    assert clauses[2].heading_path == ("II. ПОВТОРНЕ НАВЧАННЯ",)


def test_wrapped_lines_and_subitems_and_long_split() -> None:
    clauses = _seg("polozh_kaf_SAMPLE.txt")
    by_ref = {c.section_ref: c for c in clauses}
    assert by_ref["п. 2.3"].text == "Склад кафедри визначається штатним розписом Університету."
    sub = by_ref["п. 2.4"].text
    assert "1) перший" in sub and "\n3) третій" in sub        # «1)» — підпункт усередині пункту
    long = segment(clean_text((FIXTURES / "polozh_kaf_SAMPLE.txt").read_text()), max_chars=120)
    parts = [c for c in long if c.section_ref.startswith("п. 2.4")]
    assert len(parts) >= 2 and parts[1].section_ref == "п. 2.4 (продовження 2)"
    assert all(len(p.text) <= 120 for p in parts)


def test_clean_text_ocr_artifacts() -> None:
    raw = ("[[page 1]]\nДПСЯ ПП-9-6.2.2\nТекст пере-\nносу | рядка\n- 2 -\n[[page 2]]\nДПСЯ ПП-9-6.2.2\nдругий\n"
           "[[page 3]]\nДПСЯ ПП-9-6.2.2\nтретій\n17\n")
    out = clean_text(raw)
    assert "ДПСЯ" not in out          # колонтитул на кожній сторінці
    assert "переносу" in out and "| " not in out
    assert "- 2 -" not in out and "\n17" not in out


def test_ocr_number_repair() -> None:
    text = "7. ОРГАНИ УПРАВЛІННЯ\n7.2.13. Перший пункт.\n. 72.14. Другий пункт з втраченою крапкою.\n7.2.11.В Університеті кафедри.\n"
    refs = [c.section_ref for c in segment(text)]
    assert refs[:2] == ["п. 7.2.13", "п. 7.2.14"]
    assert "п. 7.2.11" in refs


def test_unstructured_text_becomes_single_section() -> None:
    clauses = segment("Просто текст без нумерації.\nЩе рядок.")
    assert len(clauses) == 1 and clauses[0].section_ref == "текст акта"


@pytest.mark.skipif(not (ROOT / "corpus" / "ocr" / "statut_puet (2).ocr.txt").exists(), reason="немає OCR Статуту")
def test_real_statute_ocr_segments_key_clauses() -> None:
    """Справжній Статут ПУЕТ (скан із папки проєкту, OCR): ключові пункти знаходяться з правильними номерами."""
    text = (ROOT / "corpus" / "ocr" / "statut_puet (2).ocr.txt").read_text(encoding="utf-8")
    clauses = segment(clean_text(text))
    by_ref = {c.section_ref: c.text for c in clauses}
    assert len(clauses) > 300
    assert "строком на п'ять років" in by_ref["п. 7.2.13"]
    assert "делегувати" in by_ref["п. 7.1.6"]
    assert "п. 7.2.14" in by_ref          # «. 72.14.» після OCR відновлено


def test_stemming_and_tokens() -> None:
    assert stem("ректора") == stem("ректором") == stem("ректору") == "ректор"
    assert stem("вченої") == stem("вчена") == stem("вчений")
    toks = tokenize("Ректор і завідувач кафедри; строком на 5 років — п'ять років")
    assert "і" not in toks and "5" in toks and stem("п'ять") in toks
    assert tokenize("п’ять") == tokenize("п'ять")            # апострофи нормалізуються
    assert token_index("ректор") == token_index("ректор") and 0 <= token_index("x") < 2**31


def test_bm25_document_and_query_vectors() -> None:
    enc = Bm25Encoder()
    d = enc.encode_document("ректор ректор кафедра")
    assert d.indices == sorted(d.indices) and len(d.indices) == 2
    w = dict(zip(d.indices, d.values, strict=True))
    assert w[token_index("ректор")] > w[token_index("кафедр")]        # TF насичується, але росте
    q = enc.encode_query("ректора кафедри ректор")
    assert len(q.indices) == 2 and set(q.values) == {1.0}
    assert enc.encode_query("і та на").indices == []


def test_fixture_files_marked_as_samples() -> None:
    for f in Path(FIXTURES).glob("*SAMPLE*.txt"):
        assert "ЗРАЗОК" in f.read_text(encoding="utf-8").splitlines()[0] or "ЗРАЗОК" in f.read_text(encoding="utf-8")[:200]
