"""Порти (інтерфейси) інфраструктури. Оркестратор залежить лише від них,
тому MinIO, Tesseract, LLM і БД підміняються в тестах і замінюються без зміни домену."""

from __future__ import annotations

import uuid
from typing import Any, Protocol

from .domain.models import (
    IncomingFile,
    IngestRecord,
    LlmCompletion,
    LlmIdentity,
    MaskingResult,
    OcrResult,
    SavedIngest,
    StoredObject,
    Uploader,
)


class ObjectStorage(Protocol):
    async def put(self, key: str, file: IncomingFile, metadata: dict[str, str]) -> StoredObject: ...

    async def delete(self, obj: StoredObject) -> None:
        """Компенсація: прибрати об'єкт, якщо транзакція в БД не вдалася."""
        ...


class OcrEngine(Protocol):
    def extract(self, file: IncomingFile) -> OcrResult:
        """Синхронний CPU-bound виклик; оркестратор запускає його в пулі потоків."""
        ...


class PiiMasker(Protocol):
    def mask(self, text: str) -> MaskingResult: ...

    def restore(self, value: str, mapping: dict[str, str]) -> str: ...


class StructuredLlm(Protocol):
    identity: LlmIdentity

    async def complete_json(
        self, messages: list[dict[str, str]], schema: dict[str, Any], schema_name: str
    ) -> LlmCompletion:
        """Chat completion зі structured output (JSON Schema). Кидає `LlmError`."""
        ...


class IngestRepository(Protocol):
    async def resolve_uploader(self, keycloak_sub: uuid.UUID, org_unit_id: uuid.UUID) -> Uploader:
        """Користувач існує, активний і має право реєструвати документи цього підрозділу."""
        ...

    async def find_duplicate(self, content_sha256: str) -> uuid.UUID | None:
        """Ідентифікатор документа, версія якого має той самий SHA-256."""
        ...

    async def save(self, record: IngestRecord) -> SavedIngest: ...
