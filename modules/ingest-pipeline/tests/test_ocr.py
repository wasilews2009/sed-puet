"""OCR на справжньому Tesseract (ukr+eng): текстовий шар, скан, змішаний PDF, поворот, захищений PDF."""

from __future__ import annotations

import hashlib
import io

import pytest
from pypdf import PdfReader, PdfWriter

from sed_ingest.adapters.ocr_engine import TesseractOcrEngine, text_layer_is_usable
from sed_ingest.domain.models import FileKind, FileRejected, IncomingFile, TextSource

from .conftest import ORDER_LINES, image_bytes, make_scan_image, make_text_pdf


def _file(data: bytes, kind: FileKind) -> IncomingFile:
    return IncomingFile(data=data, file_name=f"f.{kind.value}", kind=kind, sha256=hashlib.sha256(data).hexdigest())


def test_pdf_text_layer_used_without_ocr(ocr_engine: TesseractOcrEngine, order_pdf: bytes) -> None:
    r = ocr_engine.extract(_file(order_pdf, FileKind.PDF))
    assert r.page_count == 1 and not r.used_ocr
    page = r.pages[0]
    assert page.source is TextSource.TEXT_LAYER and page.quality == 1.0
    assert "№ 245-од" in page.text and "Петренко Олену Іванівну" in page.text


def test_scanned_image_is_recognized(ocr_engine: TesseractOcrEngine, order_png: bytes) -> None:
    r = ocr_engine.extract(_file(order_png, FileKind.PNG))
    page = r.pages[0]
    assert page.source is TextSource.OCR and r.used_ocr
    assert page.quality > 0.8
    for fragment in ("НАКАЗ", "17.10.2026", "№ 245", "Про призначення відповідальних", "РЕКТОРЕНКО"):
        assert fragment in page.text, (fragment, page.text)


def test_rotated_scan_is_auto_oriented(ocr_engine: TesseractOcrEngine) -> None:
    rotated = image_bytes(make_scan_image(ORDER_LINES, rotate=90))
    r = ocr_engine.extract(_file(rotated, FileKind.PNG))
    assert "Про призначення відповідальних" in r.pages[0].text


def test_mixed_pdf_ocrs_only_scanned_pages(ocr_engine: TesseractOcrEngine, order_pdf: bytes) -> None:
    scan_pdf = image_bytes(make_scan_image(["ДОДАТОК 1", "Список здобувачів, направлених на практику"]), "PDF")
    w = PdfWriter()
    for blob in (order_pdf, scan_pdf):
        for p in PdfReader(io.BytesIO(blob)).pages:
            w.add_page(p)
    buf = io.BytesIO()
    w.write(buf)
    r = ocr_engine.extract(_file(buf.getvalue(), FileKind.PDF))
    assert [p.source for p in r.pages] == [TextSource.TEXT_LAYER, TextSource.OCR]
    assert "ДОДАТОК" in r.pages[1].text
    assert r.page_of_offset(len(r.pages[0].text) + 5) == 2


def test_page_limit_is_reported() -> None:
    engine = TesseractOcrEngine(max_pages=1)
    pdf = make_text_pdf(ORDER_LINES)
    w = PdfWriter()
    for _ in range(3):
        w.add_page(PdfReader(io.BytesIO(pdf)).pages[0])
    buf = io.BytesIO()
    w.write(buf)
    r = engine.extract(_file(buf.getvalue(), FileKind.PDF))
    assert r.page_count == 3 and len(r.pages) == 1 and r.truncated


def test_encrypted_pdf_is_rejected(ocr_engine: TesseractOcrEngine, order_pdf: bytes) -> None:
    w = PdfWriter(clone_from=PdfReader(io.BytesIO(order_pdf)))
    w.encrypt(user_password="secret", owner_password="owner")
    buf = io.BytesIO()
    w.write(buf)
    with pytest.raises(FileRejected, match="паролем"):
        ocr_engine.extract(_file(buf.getvalue(), FileKind.PDF))


def test_corrupted_image_is_rejected(ocr_engine: TesseractOcrEngine, order_png: bytes) -> None:
    with pytest.raises(FileRejected):
        ocr_engine.extract(_file(order_png[:200], FileKind.PNG))


def test_decompression_bomb_is_rejected(order_png: bytes) -> None:
    engine = TesseractOcrEngine(max_image_pixels=10_000)
    with pytest.raises(FileRejected):
        engine.extract(_file(order_png, FileKind.PNG))


@pytest.mark.parametrize("text, ok", [
    ("НАКАЗ Про призначення відповідальних за організацію практики", True),
    ("ÍÀÊÀÇ Ïðî ïðèçíà÷åííÿ â³äïîâ³äàëüíèõ çà îðãàí³çàö³þ ïðàêòèêè", False),  # cp1251 як latin-1
    (" " * 20, False),                                     # Private Use Area
    ("12", False),
])
def test_text_layer_quality_gate(text: str, ok: bool) -> None:
    assert text_layer_is_usable(text, 20)[0] is ok


def test_word_level_confidence_exposes_misread_values(ocr_engine: TesseractOcrEngine, order_png: bytes) -> None:
    """Tesseract читає «245-од» як «245-00» з низькою впевненістю саме цього слова."""
    page = ocr_engine.extract(_file(order_png, FileKind.PNG)).pages[0]
    assert page.value_quality("17.10.2026") > 0.9
    misread = [w for w, _ in page.words if w.startswith("245")][0]
    if misread != "245-од":
        assert page.value_quality(misread) < 0.75 < page.quality
