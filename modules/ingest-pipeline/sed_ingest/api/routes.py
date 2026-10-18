"""HTTP-ендпоінт інжесту вхідного файлу."""

from __future__ import annotations

import re
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status

from sed_db.enums import AccessLevel, DocumentDirection

from ..config import Settings
from ..domain.models import FileRejected, IngestCommand
from ..domain.result import IngestResponse
from ..pipeline.file_validation import validate_upload
from ..pipeline.orchestrator import IngestPipeline
from .auth import Principal, get_principal

router = APIRouter(prefix="/api/v1/documents", tags=["Інжест документів"])

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_CHUNK = 1024 * 1024


def get_pipeline(request: Request) -> IngestPipeline:
    return request.app.state.pipeline


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


async def _read_limited(upload: UploadFile, limit: int) -> bytes:
    """Читаємо не більше limit + 1 байт: завеликий файл не потрапляє в пам'ять повністю."""
    buf = bytearray()
    while chunk := await upload.read(_CHUNK):
        buf.extend(chunk)
        if len(buf) > limit:
            raise FileRejected(f"Файл перевищує допустимий розмір {limit // (1024 * 1024)} МБ")
    return bytes(buf)


_ERRORS = {
    401: {"description": "Немає або недійсний токен Keycloak"},
    403: {"description": "Немає права реєструвати документи підрозділу"},
    415: {"description": "Формат файлу не підтримується (перевіряється за вмістом)"},
    422: {"description": "Файл пошкоджено, захищено паролем, завеликий або невалідні поля форми"},
    503: {"description": "Сховище файлів недоступне"},
}


@router.post(
    "/ingest",
    response_model=IngestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Завантажити вхідний документ і вилучити реквізити (ДСТУ 4163:2020)",
    description=(
        "Зберігає оригінал у MinIO, розпізнає текст (текстовий шар PDF або Tesseract ukr+eng), "
        "вилучає реквізити локальною LLM зі structured output і повертає їх із confidence score. "
        "Створюється картка документа в статусі «чернетка». Реквізити з впевненістю < 0.85 "
        "потрапляють у чергу ручної валідації; юридичної сили документ ШІ не надає."
    ),
    responses=_ERRORS,  # type: ignore[arg-type]
)
async def ingest_document(
    request: Request,
    file: Annotated[UploadFile, File(description="PDF або зображення (PNG, JPEG, TIFF, WEBP)")],
    org_unit_id: Annotated[uuid.UUID, Form(description="Підрозділ, у якому реєструється документ")],
    principal: Annotated[Principal, Depends(get_principal)],
    pipeline: Annotated[IngestPipeline, Depends(get_pipeline)],
    settings: Annotated[Settings, Depends(get_settings)],
    direction: Annotated[DocumentDirection | None, Form(description="За замовчуванням — з виду документа")] = None,
    access_level: Annotated[AccessLevel | None, Form(description="Гриф доступу; може бути посилено автоматично")] = None,
    correspondent_name: Annotated[str | None, Form(max_length=512, description="Обов'язково для вхідних")] = None,
) -> IngestResponse:
    correspondent = (correspondent_name or "").strip() or None
    if direction is DocumentDirection.INCOMING and correspondent is None:
        raise HTTPException(422, "Для вхідного документа обов'язковий кореспондент")

    data = await _read_limited(file, settings.max_file_bytes)
    incoming = validate_upload(data, file.filename, settings.max_file_bytes)

    header_id = request.headers.get("X-Request-ID", "")
    request_id = header_id if _REQUEST_ID.match(header_id) else uuid.uuid4().hex
    cmd = IngestCommand(
        file=incoming, uploader_sub=principal.sub, org_unit_id=org_unit_id, direction=direction,
        access_level=access_level, correspondent_name=correspondent, request_id=request_id,
    )
    return await pipeline.run(cmd)
