"""Отримання тексту акта з файлу: UTF-8 текст, PDF з текстовим шаром або скан (Tesseract ukr+eng).

Статут і положення ПУЕТ у папці проєкту — скани з «фальшивим» текстовим шаром: кирилиця
закодована латинськими двійниками («3MiCT» замість «ЗМІСТ»). Такий шар не проходить перевірку
частки кирилиці, і сторінка розпізнається OCR. Для юридично значущого корпусу рекомендований
шлях: `extract-text` → вичитка людиною → індексація вичитаного .txt з `text_quality = 1.0`.
"""

from __future__ import annotations

import os
import re
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

_CYR = re.compile(r"[а-яіїєґА-ЯІЇЄҐ]")
_LETTERS = re.compile(r"[^\W\d_]", re.UNICODE)


@dataclass(frozen=True, slots=True)
class ExtractedText:
    text: str                  # сторінки розділені маркерами «[[page N]]»
    quality: float             # 1.0 — текст/текстовий шар; OCR — середня впевненість слів
    pages: int
    ocr_pages: int


def text_layer_ok(text: str, min_letters: int = 80, min_cyrillic: float = 0.5) -> bool:
    letters = len(_LETTERS.findall(text))
    if letters < min_letters:
        return False
    return len(_CYR.findall(text)) / letters >= min_cyrillic


def _ocr_page(args: tuple[str, int, int, str]) -> tuple[int, str, float]:
    import pypdfium2 as pdfium
    import pytesseract

    path, index, dpi, lang = args
    pdf = pdfium.PdfDocument(path)
    try:
        img = pdf[index].render(scale=dpi / 72).to_pil().convert("L")
    finally:
        pdf.close()
    data = pytesseract.image_to_data(img, lang=lang, output_type=pytesseract.Output.DICT,
                                     config="--psm 3 -c preserve_interword_spaces=1")
    lines: dict[tuple[int, int, int], list[str]] = {}
    confs: list[float] = []
    for i, word in enumerate(data["text"]):
        if not word.strip():
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append(word)
        c = float(data["conf"][i])
        if c >= 0:
            confs.append(c / 100)
    text, prev = [], None
    for key, words in lines.items():
        if prev is not None and key[:2] != prev[:2]:
            text.append("")                      # новий абзац
        text.append(" ".join(words))
        prev = key
    return index, "\n".join(text), (sum(confs) / len(confs) if confs else 0.0)


def read_act_file(path: Path, *, dpi: int = 300, lang: str = "ukr+eng", workers: int | None = None,
                  force_ocr: bool = False) -> ExtractedText:
    """`force_ocr` — ігнорувати текстовий шар (буває «битий»: зниклі останні літери слів)."""
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md"}:
        return ExtractedText(path.read_text(encoding="utf-8"), 1.0, 1, 0)
    if suffix != ".pdf":
        raise ValueError(f"Непідтримуваний формат акта: {suffix} (потрібен .pdf, .txt або .md)")

    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(path))
    try:
        layers = [pdf[i].get_textpage().get_text_range() for i in range(len(pdf))]
    finally:
        pdf.close()
    need_ocr = [i for i, t in enumerate(layers) if force_ocr or not text_layer_ok(t)]
    texts: dict[int, tuple[str, float]] = {i: (t, 1.0) for i, t in enumerate(layers) if i not in need_ocr}
    if need_ocr:
        # Tesseract сам багатопотоковий; обмежуємо, щоб не перевантажити CPU процесами × потоками.
        os.environ.setdefault("OMP_THREAD_LIMIT", "1")
        with ProcessPoolExecutor(max_workers=workers or os.cpu_count() or 2) as ex:
            for idx, text, q in ex.map(_ocr_page, [(str(path), i, dpi, lang) for i in need_ocr]):
                texts[idx] = (text, q)
    parts, weighted, total = [], 0.0, 0
    for i in range(len(layers)):
        t, q = texts[i]
        parts.append(f"[[page {i + 1}]]\n{t}")
        weighted += q * len(t)
        total += len(t)
    return ExtractedText("\n".join(parts), round(weighted / total, 4) if total else 0.0, len(layers), len(need_ocr))
