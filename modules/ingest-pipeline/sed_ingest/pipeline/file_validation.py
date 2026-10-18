"""Перевірка вхідного файлу за вмістом (magic bytes), а не за розширенням чи Content-Type клієнта."""

from __future__ import annotations

import hashlib
import re
import unicodedata

from ..domain.models import FileKind, FileRejected, IncomingFile, UnsupportedFile

_SIGNATURES: tuple[tuple[bytes, FileKind], ...] = (
    (b"%PDF-", FileKind.PDF),
    (b"\x89PNG\r\n\x1a\n", FileKind.PNG),
    (b"\xff\xd8\xff", FileKind.JPEG),
    (b"II*\x00", FileKind.TIFF),
    (b"MM\x00*", FileKind.TIFF),
)


def sniff_kind(data: bytes) -> FileKind | None:
    head = data[:1024]
    for sig, kind in _SIGNATURES:
        if head.startswith(sig):
            return kind
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return FileKind.WEBP
    # PDF допускає «сміття» до заголовка в межах першого кілобайта (ISO 32000-1, 7.5.2)
    if b"%PDF-" in head:
        return FileKind.PDF
    return None


_UNSAFE = re.compile(r"[^\w.\- ()]+", re.UNICODE)


def safe_file_name(name: str | None, kind: FileKind) -> str:
    """Ім'я файлу для картки: без шляхів, керівних символів і з правильним розширенням."""
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = unicodedata.normalize("NFC", base)
    base = _UNSAFE.sub("_", base).strip(" ._") or "document"
    stem = (base.rsplit(".", 1)[0] if "." in base else base).strip(" ._") or "document"
    ext = "jpg" if kind is FileKind.JPEG else ("tif" if kind is FileKind.TIFF else kind.value)
    return f"{stem[:200]}.{ext}"


def validate_upload(data: bytes, file_name: str | None, max_bytes: int) -> IncomingFile:
    if not data:
        raise FileRejected("Файл порожній")
    if len(data) > max_bytes:
        raise FileRejected(f"Файл перевищує допустимий розмір {max_bytes // (1024 * 1024)} МБ")
    kind = sniff_kind(data)
    if kind is None:
        raise UnsupportedFile("Підтримуються лише PDF та зображення (PNG, JPEG, TIFF, WEBP)")
    return IncomingFile(
        data=data,
        file_name=safe_file_name(file_name, kind),
        kind=kind,
        sha256=hashlib.sha256(data).hexdigest(),
    )
