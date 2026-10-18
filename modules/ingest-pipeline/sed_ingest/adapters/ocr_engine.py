"""OCR: текстовий шар PDF першим, Tesseract (ukr+eng) — для сканів і зіпсованих текстових шарів.

Рішення приймається посторінково: у «змішаному» PDF (надруковані сторінки + скан додатка
з підписами) кожна сторінка отримує своє джерело тексту та свою оцінку якості.
"""

from __future__ import annotations

import io
import logging
import re
import time
from collections.abc import Iterator

import pypdfium2 as pdfium
import pytesseract
from PIL import Image, ImageOps, ImageSequence, UnidentifiedImageError

from ..domain.models import FileKind, FileRejected, IncomingFile, OcrResult, PageText, TextSource

log = logging.getLogger(__name__)

_CYR = re.compile(r"[Ѐ-ӿ]")
_LATIN1_SUP = re.compile(r"[À-ÿ]")   # «Íàêàç» — cp1251, прочитаний як latin-1
_LETTERS = re.compile(r"[^\W\d_]", re.UNICODE)
_BROKEN = re.compile(r"[�-]")  # replacement char, Private Use Area (шрифти без ToUnicode)
_ROTATE = re.compile(r"Rotate:\s+(\d+)")
# Типова помилка Tesseract на українських документах: знак «№» читається як «Ne», «No», «N2».
_NUMERO = re.compile(r"(?<!\w)N[eoº°2](?=\s?\d)")


def text_layer_is_usable(text: str, min_letters: int) -> tuple[bool, str | None]:
    """Чи можна довіряти текстовому шару сторінки. Повертає (так/ні, причина відмови)."""
    letters = len(_LETTERS.findall(text))
    if letters < min_letters:
        return False, "текстовий шар відсутній або замалий"
    broken = len(_BROKEN.findall(text))
    if broken > 0.05 * letters:
        return False, "текстовий шар зіпсований (шрифти без таблиці Unicode)"
    cyr, lat1 = len(_CYR.findall(text)), len(_LATIN1_SUP.findall(text))
    if lat1 > 0.3 * letters and lat1 > cyr:
        return False, "текстовий шар у неправильному кодуванні (cp1251 → latin-1)"
    return True, None


def _normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"-\n(?=[а-яіїєґa-z])", "", text)   # перенос слова зі знаком «-»
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class TesseractOcrEngine:
    """Реалізація порту `OcrEngine`."""

    def __init__(
        self,
        lang: str = "ukr+eng",
        dpi: int = 300,
        max_pages: int = 30,
        page_timeout_s: int = 60,
        text_layer_min_chars: int = 40,
        max_image_pixels: int = 60_000_000,
    ) -> None:
        self.lang = lang
        self.dpi = dpi
        self.max_pages = max_pages
        self.page_timeout_s = page_timeout_s
        self.text_layer_min_chars = text_layer_min_chars
        # Власний ліміт на екземпляр (глобальний Image.MAX_IMAGE_PIXELS лишається як другий рубіж).
        self.max_image_pixels = max_image_pixels
        self._version = str(pytesseract.get_tesseract_version())

    @property
    def version(self) -> str:
        return self._version

    # --- публічне API --------------------------------------------------------------

    def extract(self, file: IncomingFile) -> OcrResult:
        started = time.monotonic()
        if file.kind is FileKind.PDF:
            pages, total = self._extract_pdf(file.data)
            engine = f"pdf-text-layer+tesseract {self._version} {self.lang}"
        else:
            pages, total = self._extract_image(file.data)
            engine = f"tesseract {self._version} {self.lang}"
        return OcrResult(
            pages=tuple(pages),
            engine=engine,
            engine_version=self._version,
            page_count=total,
            truncated=total > len(pages),
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    # --- PDF -----------------------------------------------------------------------

    def _extract_pdf(self, data: bytes) -> tuple[list[PageText], int]:
        try:
            pdf = pdfium.PdfDocument(data)
        except pdfium.PdfiumError as exc:
            msg = str(exc).lower()
            if "password" in msg:
                raise FileRejected("PDF захищено паролем — зніміть захист перед завантаженням") from exc
            raise FileRejected("PDF пошкоджено або він не відповідає формату") from exc
        try:
            total = len(pdf)
            if total == 0:
                raise FileRejected("PDF не містить сторінок")
            pages: list[PageText] = []
            for i in range(min(total, self.max_pages)):
                page = pdf[i]
                try:
                    textpage = page.get_textpage()
                    try:
                        raw = textpage.get_text_bounded()
                    finally:
                        textpage.close()
                    layer = _normalize_text(raw)
                    ok, reason = text_layer_is_usable(layer, self.text_layer_min_chars)
                    if ok:
                        pages.append(PageText(i + 1, layer, TextSource.TEXT_LAYER, 1.0))
                        continue
                    bitmap = page.render(scale=self.dpi / 72, grayscale=True)
                    image = bitmap.to_pil()
                    bitmap.close()
                    pages.append(self._ocr_page(image, i + 1, note=reason))
                finally:
                    page.close()
            return pages, total
        finally:
            pdf.close()

    # --- Зображення ----------------------------------------------------------------

    def _extract_image(self, data: bytes) -> tuple[list[PageText], int]:
        try:
            img = Image.open(io.BytesIO(data))
            img.verify()                    # структурна перевірка без декодування растру
            img = Image.open(io.BytesIO(data))      # лінивий: розмір відомий із заголовка, растр ще не декодовано
            if img.width * img.height > self.max_image_pixels:
                raise FileRejected("Зображення завелике (захист від decompression bomb)")
        except Image.DecompressionBombError as exc:
            raise FileRejected("Зображення завелике (захист від decompression bomb)") from exc
        except (UnidentifiedImageError, OSError, SyntaxError) as exc:
            raise FileRejected("Зображення пошкоджено або формат не підтримується") from exc
        total = getattr(img, "n_frames", 1)
        pages = [self._ocr_page(frame, n) for n, frame in self._frames(img)]
        return pages, total

    def _frames(self, img: Image.Image) -> Iterator[tuple[int, Image.Image]]:
        for n, frame in enumerate(ImageSequence.Iterator(img), start=1):
            if n > self.max_pages:
                return
            yield n, ImageOps.exif_transpose(frame.copy())

    # --- Tesseract -----------------------------------------------------------------

    def _prepare(self, image: Image.Image) -> Image.Image:
        if image.width * image.height > self.max_image_pixels:
            raise FileRejected("Сторінка завелика для розпізнавання")
        img = image.convert("L")
        img = ImageOps.autocontrast(img, cutoff=1)
        if img.width < 1200:                # фото з телефона / скан 100 dpi: збільшуємо для Tesseract
            img = img.resize((img.width * 2, img.height * 2), Image.Resampling.LANCZOS)
        return self._deskew_orientation(img)

    def _deskew_orientation(self, img: Image.Image) -> Image.Image:
        """Сторінка, відсканована догори дриґом чи боком (OSD Tesseract)."""
        try:
            osd = pytesseract.image_to_osd(img, config="--psm 0 -c min_characters_to_try=10",
                                           timeout=self.page_timeout_s)
        except (pytesseract.TesseractError, RuntimeError):
            return img                      # замало тексту для OSD — лишаємо як є
        m = _ROTATE.search(osd)
        angle = int(m.group(1)) if m else 0
        return img.rotate(-angle, expand=True) if angle else img

    def _ocr_page(self, image: Image.Image, page_no: int, note: str | None = None) -> PageText:
        img = self._prepare(image)
        try:
            data = pytesseract.image_to_data(
                img, lang=self.lang, config="--oem 1 --psm 3",
                output_type=pytesseract.Output.DICT, timeout=self.page_timeout_s,
            )
        except RuntimeError as exc:         # pytesseract кидає RuntimeError на timeout
            log.warning("OCR timeout on page %s", page_no)
            return PageText(page_no, "", TextSource.EMPTY, 0.0, note="перевищено час розпізнавання")
        text, quality, words = _assemble(data)
        if not text:
            return PageText(page_no, "", TextSource.EMPTY, 0.0, note=note or "текст не розпізнано")
        text = _NUMERO.sub("№", _normalize_text(text))
        return PageText(page_no, text, TextSource.OCR, quality, note=note, words=words)


def _assemble(data: dict[str, list]) -> tuple[str, float, tuple[tuple[str, float], ...]]:  # type: ignore[type-arg]
    """Збирає текст із `image_to_data` зі збереженням рядків/абзаців і рахує якість."""
    lines: dict[tuple[int, int, int], list[str]] = {}
    weighted, weight = 0.0, 0
    scored: list[tuple[str, float]] = []
    for i, word in enumerate(data["text"]):
        word = (word or "").strip()
        conf = float(data["conf"][i])
        if not word or conf < 0:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append(word)
        scored.append((word, conf / 100.0))
        weighted += conf * len(word)
        weight += len(word)
    out: list[str] = []
    prev_par: tuple[int, int] | None = None
    for (block, par, _), words in sorted(lines.items()):
        if prev_par is not None and (block, par) != prev_par:
            out.append("")
        out.append(" ".join(words))
        prev_par = (block, par)
    quality = (weighted / weight / 100.0) if weight else 0.0
    return "\n".join(out).strip(), round(quality, 4), tuple(scored)
