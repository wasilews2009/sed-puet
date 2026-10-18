"""Документи: довідники, список, картка, файл, ручна валідація результатів ШІ, нормативний аудит версії.

Завантаження нового документа — `POST /api/v1/documents/ingest` модуля `sed_ingest`;
перевірка довільного тексту і пошук норм — `/api/v1/compliance/check`, `/api/v1/regulations/*` модуля `sed_rag`.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any
from urllib.parse import quote

import anyio
from fastapi import APIRouter, Body, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from sed_db.enums import DocumentStatus
from sed_db.models import Document, DocumentType, DocumentVersion
from sed_db.schemas.common import InputModel

from ..adapters.db import audit_event, set_actor
from ..domain.errors import InvalidInput, NotFound
from ..domain.labels import FIELD_LABELS_UK, all_labels
from .deps import CurrentActor, RequestId, Scope, Services, require, reviewable, viewable
from .schemas import (
    DictionariesOut,
    DocumentCard,
    DocumentPage,
    DocumentSort,
    DocumentTypeOut,
    FieldDecision,
    FieldReviewIn,
    FindingResolutionIn,
    InboxItem,
    ModulesOut,
    OrgUnitOut,
    ReviewQueueItem,
)

router = APIRouter(prefix="/api/v1")

_ERR = {401: {"description": "Немає або недійсний токен"},
        403: {"description": "Немає права на дію"},
        404: {"description": "Документ не знайдено або немає доступу"},
        409: {"description": "Картку змінив інший користувач"},
        422: {"description": "Некоректні дані"}}


@router.get("/dictionaries", response_model=DictionariesOut, tags=["Довідники"],
            summary="Підрозділи, види документів, підписи статусів українською")
async def dictionaries(_: Scope, svc: Services) -> DictionariesOut:
    async with svc.documents.sessionmaker() as s:
        types = (await s.scalars(select(DocumentType).where(DocumentType.is_active.is_(True))
                                 .order_by(DocumentType.name_uk))).all()
    return DictionariesOut(
        org_units=[OrgUnitOut(id=u.id, parent_id=u.parent_id, kind=u.kind, code=u.code, name=u.name_uk,
                              short_name=u.short_name_uk) for u in await svc.users.org_units()],
        document_types=[DocumentTypeOut(id=t.id, code=t.code, name=t.name_uk, requires_kep=t.requires_kep)
                        for t in types],
        labels=all_labels(), field_labels=FIELD_LABELS_UK,
        modules=ModulesOut(ingest=svc.settings.enable_ingest, compliance=svc.compliance is not None,
                           routing=svc.routing is not None, kep_mock=svc.kep_mock),
    )


@router.get("/documents", response_model=DocumentPage, tags=["Документи"], responses=_ERR,
            summary="Документи, доступні користувачу")
async def list_documents(
    scope: Scope, svc: Services,
    q: Annotated[str | None, Query(max_length=200, description="Пошук у заголовку, індексі, короткому змісті")] = None,
    status_: Annotated[DocumentStatus | None, Query(alias="status")] = None,
    type_code: Annotated[str | None, Query(max_length=64)] = None,
    org_unit_id: uuid.UUID | None = None,
    needs_review: Annotated[bool | None, Query(description="Лише ті, що чекають ручної валідації ШІ")] = None,
    mine: Annotated[bool, Query(description="Лише мої (я автор)")] = False,
    sort: DocumentSort = DocumentSort.UPDATED_DESC,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> DocumentPage:
    return await svc.documents.list(scope, q=q, status=status_, type_code=type_code, org_unit_id=org_unit_id,
                                    needs_review=needs_review, mine=mine, sort=sort, limit=limit, offset=offset)


@router.get("/documents/{document_id}", response_model=DocumentCard, tags=["Документи"], responses=_ERR,
            summary="Картка документа: реквізити ШІ, комплаєнс, маршрут, підписи, історія")
async def get_card(document_id: uuid.UUID, scope: Scope, svc: Services, rid: RequestId) -> DocumentCard:
    facts = await viewable(svc, scope, document_id)
    return await svc.documents.card(scope, facts, request_id=rid)


@router.get("/documents/{document_id}/versions/{version_id}/file", tags=["Документи"], responses=_ERR,
            response_class=StreamingResponse, summary="Оригінал файлу версії (з перевіркою доступу і записом у журнал)")
async def download(document_id: uuid.UUID, version_id: uuid.UUID, scope: Scope, svc: Services, rid: RequestId,
                   inline: bool = True) -> StreamingResponse:
    await viewable(svc, scope, document_id)
    files = require(svc.files, "Сховище файлів")
    async with svc.documents.sessionmaker() as s, s.begin():
        v = await s.scalar(select(DocumentVersion).where(DocumentVersion.id == version_id,
                                                         DocumentVersion.document_id == document_id))
        if v is None:
            raise NotFound("Версію не знайдено")
        await set_actor(s, scope.user_id, rid)
        await audit_event(s, "file.download", "document_versions", v.id, document_id,
                          {"file_name": v.file_name, "sha256": v.content_sha256})
    chunks = await anyio.to_thread.run_sync(files.open, v.storage_bucket, v.storage_key, v.storage_version_id)

    async def body():  # noqa: ANN202
        it = iter(chunks)
        while True:
            chunk = await anyio.to_thread.run_sync(next, it, None)
            if chunk is None:
                break
            yield chunk

    disposition = "inline" if inline else "attachment"
    return StreamingResponse(body(), media_type=v.mime_type, headers={
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(v.file_name)}",
        "Content-Length": str(v.size_bytes), "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff", "X-Content-SHA256": v.content_sha256,
    })


# --- Human-in-the-loop -----------------------------------------------------------------------

@router.post("/documents/{document_id}/fields/{field_id}/review", status_code=status.HTTP_204_NO_CONTENT,
             tags=["Ручна валідація"], responses=_ERR,
             summary="Підтвердити, виправити або відхилити реквізит, вилучений ШІ")
async def review_field(document_id: uuid.UUID, field_id: uuid.UUID, body: FieldReviewIn, scope: Scope,
                       svc: Services, rid: RequestId) -> None:
    """Обов'язково для реквізитів з впевненістю < 0.85. Підтверджене значення потрапляє
    в `documents.requisites` (картку); сирий вихід моделі лишається в `ai_extracted_fields`."""
    await reviewable(svc, scope, document_id)
    await svc.review.review_field(scope.user_id, document_id, field_id, body, rid)


class RequisitesIn(InputModel):
    values: dict[str, Any] = Field(description="Ключі: " + ", ".join(FIELD_LABELS_UK))
    row_version: int | None = None


class AcceptConfidentIn(InputModel):
    row_version: int | None = None


class AcceptConfidentOut(BaseModel):
    accepted: int


@router.post("/documents/{document_id}/requisites/accept-confident", response_model=AcceptConfidentOut,
             tags=["Ручна валідація"], responses=_ERR,
             summary="Перенести в картку реквізити, вилучені ШІ з впевненістю ≥ 0.85")
async def accept_confident(document_id: uuid.UUID, body: AcceptConfidentIn, scope: Scope, svc: Services,
                           rid: RequestId) -> AcceptConfidentOut:
    """Значення з високою впевненістю не потребують перевірки, але в картку їх переносить людина."""
    await reviewable(svc, scope, document_id)
    return AcceptConfidentOut(accepted=await svc.review.accept_confident(scope.user_id, document_id,
                                                                         body.row_version, rid))


@router.put("/documents/{document_id}/requisites", status_code=status.HTTP_204_NO_CONTENT,
            tags=["Ручна валідація"], responses=_ERR,
            summary="Ввести реквізити вручну (ШІ не впорався або модель недоступна)")
async def enter_requisites(document_id: uuid.UUID, body: RequisitesIn, scope: Scope, svc: Services,
                           rid: RequestId) -> None:
    await reviewable(svc, scope, document_id)
    await svc.review.enter_requisites(scope.user_id, document_id, body.values, body.row_version, rid)


@router.post("/documents/{document_id}/findings/{finding_id}/resolution", status_code=status.HTTP_204_NO_CONTENT,
             tags=["Ручна валідація"], responses=_ERR, summary="Розглянути зауваження комплаєнсу")
async def resolve_finding(document_id: uuid.UUID, finding_id: uuid.UUID, body: FindingResolutionIn, scope: Scope,
                          svc: Services, rid: RequestId) -> None:
    await reviewable(svc, scope, document_id)
    await svc.review.resolve_finding(scope.user_id, document_id, finding_id, body, rid)


class RunReviewIn(InputModel):
    decision: FieldDecision
    comment: str | None = Field(None, max_length=2000)


@router.post("/documents/{document_id}/ai-runs/{run_id}/review", status_code=status.HTTP_204_NO_CONTENT,
             tags=["Ручна валідація"], responses=_ERR,
             summary="Підтвердити або відхилити результат ШІ в цілому (напр. звіт комплаєнсу)")
async def review_run(document_id: uuid.UUID, run_id: uuid.UUID, body: RunReviewIn, scope: Scope, svc: Services,
                     rid: RequestId) -> None:
    await reviewable(svc, scope, document_id)
    if body.decision is FieldDecision.REJECT and not (body.comment and body.comment.strip()):
        raise InvalidInput("Вкажіть, чому результат ШІ відхилено")
    await svc.review.review_run(scope.user_id, document_id, run_id, body.decision, body.comment, rid)


class ComplianceRunIn(InputModel):
    as_of: date | None = Field(None, description="Дата, на яку оцінюється чинність норм")


@router.post("/documents/{document_id}/compliance-checks", status_code=status.HTTP_201_CREATED,
             tags=["Нормативний аудит"], responses={**_ERR, 503: {"description": "Qdrant / LLM недоступні"}},
             summary="Перевірити поточну версію на відповідність Статуту, положенням ПУЕТ і наказам МОН")
async def run_compliance(document_id: uuid.UUID, scope: Scope, actor: CurrentActor, svc: Services,
                         rid: RequestId, body: Annotated[ComplianceRunIn | None, Body()] = None) -> dict[str, Any]:
    """Синхронний виклик графа LangGraph модуля `sed_rag` (хвилини на великих документах).
    Результат пишеться в `ai_runs` + `compliance_*`; картка показує його з цитатами і статусами зауважень."""
    service = require(svc.compliance, "Нормативний аудит (RAG)")
    await reviewable(svc, scope, document_id)
    async with svc.documents.sessionmaker() as s:
        version_id = await s.scalar(select(Document.current_version_id).where(Document.id == document_id))
    if version_id is None:
        raise NotFound("У документа немає версії")
    report = await service.check_version(actor.keycloak_sub, version_id, as_of=body.as_of if body else None,
                                         request_id=rid)
    return report.model_dump(mode="json")


# --- Черги ---------------------------------------------------------------------------------------

@router.get("/review-queue", response_model=list[ReviewQueueItem], tags=["Ручна валідація"],
            summary="Результати ШІ з впевненістю < 0.85, які чекають мого рішення")
async def review_queue(scope: Scope, svc: Services) -> list[ReviewQueueItem]:
    return await svc.documents.review_queue(scope)


@router.get("/approvals/inbox", response_model=list[InboxItem], tags=["Погодження"],
            summary="Мої активні кроки погодження (візи, підписи)")
async def inbox(scope: Scope, svc: Services) -> list[InboxItem]:
    return await svc.documents.inbox(scope.user_id)
