"""Спільні фікстури: тестові документи, фейки зовнішніх сервісів (MinIO, LLM, БД), JWT.

Tesseract використовується справжній (ukr+eng) — OCR перевіряється на згенерованих сканах.
"""

from __future__ import annotations

import io
import re
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageDraw, ImageFont

sys.dont_write_bytecode = True   # не залишати __pycache__ у спільній папці (зокрема в ../db-schema)
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT.parent / "db-schema")]

from sed_ingest.domain.models import (  # noqa: E402
    AccessDenied,
    IncomingFile,
    IngestRecord,
    LlmCompletion,
    LlmIdentity,
    SavedIngest,
    StoredObject,
    Uploader,
)
from sed_ingest.pipeline.prompts import DOC_CLOSE, DOC_OPEN  # noqa: E402

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

ORDER_LINES = [
    "ПОЛТАВСЬКИЙ УНІВЕРСИТЕТ ЕКОНОМІКИ І ТОРГІВЛІ",
    "НАКАЗ",
    "17.10.2026 м. Полтава № 245-од",
    "Про призначення відповідальних за організацію практики",
    "НАКАЗУЮ:",
    "1. Призначити відповідальною за організацію практики доцента",
    "кафедри економіки Петренко Олену Іванівну.",
    "2. Контроль за виконанням наказу покласти на проректора",
    "з науково-педагогічної роботи Коваленка Андрія Петровича.",
    "Ректор Іван РЕКТОРЕНКО",
]
ORDER_TEXT = "\n".join(ORDER_LINES)


# --- генератори файлів -------------------------------------------------------------

def make_text_pdf(lines: list[str]) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    if "DejaVu" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("DejaVu", FONT))
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("DejaVu", 12)
    y = 800
    for line in lines:
        c.drawString(60, y, line)
        y -= 22
    c.save()
    return buf.getvalue()


def make_scan_image(lines: list[str], *, rotate: int = 0) -> Image.Image:
    font = ImageFont.truetype(FONT, 34)
    img = Image.new("L", (1800, 120 + 60 * len(lines)), 255)
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        draw.text((80, 60 + 60 * i), line, font=font, fill=0)
    return img.rotate(rotate, expand=True, fillcolor=255) if rotate else img


def image_bytes(img: Image.Image, fmt: str = "PNG") -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt, **({"resolution": 200.0} if fmt == "PDF" else {}))
    return buf.getvalue()


@pytest.fixture(scope="session")
def order_pdf() -> bytes:
    return make_text_pdf(ORDER_LINES)


@pytest.fixture(scope="session")
def order_png() -> bytes:
    return image_bytes(make_scan_image(ORDER_LINES))


@pytest.fixture(scope="session")
def ocr_engine():  # type: ignore[no-untyped-def]
    from sed_ingest.adapters.ocr_engine import TesseractOcrEngine
    return TesseractOcrEngine(dpi=200)


# --- «LLM», що відповідає за текстом промпту ---------------------------------------

def document_block(messages: list[dict[str, str]]) -> str:
    user = [m for m in messages if m["role"] == "user"][0]["content"]
    return user.split(DOC_OPEN, 1)[1].split(DOC_CLOSE, 1)[0]


def _line(text: str, pattern: str) -> str | None:
    for line in text.splitlines():
        if re.search(pattern, line):
            return line.strip()
    return None


def order_responder(messages: list[dict[str, str]]) -> dict[str, Any]:
    """Поводиться як сумлінна модель: бере значення й цитати з того тексту, який реально бачить."""
    doc = document_block(messages)
    head = _line(doc, r"№") or ""
    date_m = re.search(r"\d{2}\.\d{2}\.\d{4}", head)
    num_m = re.search(r"№\s*(\S+)", head)
    title = _line(doc, r"^Про ")
    sign = _line(doc, r"^Ректор") or ""
    tokens = re.findall(r"\[ОСОБА_\d+\]", doc)
    executor_ev = _line(doc, r"Петренко|ОСОБА_1\]")
    control_ev = _line(doc, r"Коваленка|ОСОБА_2\]")
    return {
        "document_type": {"value": "order_main", "evidence": "НАКАЗ", "confidence": 0.95},
        "document_date": {"value": date_m.group(0) if date_m else None, "evidence": head, "confidence": 0.95},
        "registration_number": {"value": num_m.group(1) if num_m else None, "evidence": head, "confidence": 0.95},
        "author": {"value": {"full_name": tokens[-1] if tokens else None, "position": "Ректор",
                             "org_unit": None, "evidence": sign}, "confidence": 0.95},
        "title": {"value": title, "evidence": title, "confidence": 0.95},
        "summary": {"value": "Призначено відповідальних за організацію практики, контроль покладено на проректора.",
                    "confidence": 0.9},
        "interested_parties": {"value": [
            {"full_name": tokens[0] if tokens else None, "position": "доцент", "org_unit": "кафедра економіки",
             "evidence": executor_ev, "role": "executor"},
            {"full_name": tokens[1] if len(tokens) > 1 else None, "position": "проректор", "org_unit": None,
             "evidence": control_ev, "role": "controller"},
        ], "confidence": 0.95},
    }


class ScriptedLlm:
    """Фейк порту `StructuredLlm`: послідовність відповідей (dict, callable або виняток)."""

    identity = LlmIdentity("test-llm", "1.0", "vllm")

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[list[dict[str, str]]] = []

    async def complete_json(self, messages, schema, schema_name) -> LlmCompletion:  # type: ignore[no-untyped-def]
        self.calls.append(messages)
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        content = r(messages) if callable(r) else r
        return LlmCompletion(content=content, model="test-llm", tokens_in=1000, tokens_out=200,
                             latency_ms=50, attempts=1)


# --- фейки MinIO і БД --------------------------------------------------------------

class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []

    async def put(self, key: str, file: IncomingFile, metadata: dict[str, str]) -> StoredObject:
        self.objects[key] = file.data
        return StoredObject(bucket="sed-documents", key=key, version_id="v-1", etag="e")

    async def delete(self, obj: StoredObject) -> None:
        self.objects.pop(obj.key, None)
        self.deleted.append(obj.key)


class FakeRepository:
    def __init__(self, allowed_units: set[uuid.UUID] | None = None) -> None:
        self.user_id = uuid.uuid4()
        self.allowed_units = allowed_units
        self.saved: list[IngestRecord] = []
        self.fail_with: Exception | None = None
        self.duplicates: dict[str, uuid.UUID] = {}

    async def resolve_uploader(self, keycloak_sub: uuid.UUID, org_unit_id: uuid.UUID) -> Uploader:
        if self.allowed_units is not None and org_unit_id not in self.allowed_units:
            raise AccessDenied("Немає права реєструвати документи цього підрозділу")
        return Uploader(user_id=self.user_id, org_unit_id=org_unit_id)

    async def find_duplicate(self, content_sha256: str) -> uuid.UUID | None:
        return self.duplicates.get(content_sha256)

    async def save(self, record: IngestRecord) -> SavedIngest:
        if self.fail_with:
            raise self.fail_with
        self.saved.append(record)
        from sed_db.enums import AccessLevel, DocumentDirection
        return SavedIngest(
            document_id=record.document_id, version_id=record.version_id, ocr_run_id=uuid.uuid4(),
            extraction_run_id=uuid.uuid4(), document_type_code="ORDER_MAIN" if record.kind else "UNSORTED",
            access_level=record.access_level or AccessLevel.INTERNAL,
            direction=record.direction or DocumentDirection.INTERNAL,
        )


Responder = Callable[[list[dict[str, str]]], dict[str, Any]]
