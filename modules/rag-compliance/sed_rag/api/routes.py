"""HTTP-ендпоінти нормативного аудиту і корпусу норм."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from sed_db.enums import RegulatoryLevel, RegulatoryStatus
from sed_ingest.api.auth import Principal, get_principal

from ..domain.models import ActMeta, Coverage
from ..domain.report import ComplianceReport
from ..ingestion.service import CorpusIngestor, IngestStatus
from ..service import ComplianceService

router = APIRouter(prefix="/api/v1")

_ERRORS = {
    401: {"description": "Немає або недійсний токен"},
    403: {"description": "Немає доступу"},
    422: {"description": "Некоректний запит"},
    503: {"description": "Векторна БД, сервер ембедингів або БД недоступні"},
}


def _service(request: Request) -> ComplianceService:
    return request.app.state.service  # type: ignore[no-any-return]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CheckTextRequest(_In):
    text: str = Field(min_length=20, max_length=60_000, description="Текст проєкту документа")
    as_of: date | None = Field(None, description="Дата, на яку оцінюється чинність норм (за замовчуванням — сьогодні)")


class CheckVersionRequest(_In):
    as_of: date | None = Field(None, description="За замовчуванням — дата реєстрації документа або сьогодні")


@router.post("/compliance/check", response_model=ComplianceReport, tags=["Нормативний аудит"],
             summary="Перевірити текст проєкту (без збереження)", responses=_ERRORS)
async def check_text(body: CheckTextRequest, request: Request,
                     _: Annotated[Principal, Depends(get_principal)]) -> ComplianceReport:
    """Шукає релевантні пункти Статуту, положень ПУЕТ, законів і наказів МОН, оцінює кожен пункт
    проєкту і повертає звіт з переліком пунктів актів, які можуть бути порушені. Підходить для
    перевірки чернетки в редакторі чи проєкту, згенерованого асистентом; у БД нічого не пише."""
    return await _service(request).check_text(body.text, body.as_of)


@router.post("/documents/versions/{version_id}/compliance-checks", response_model=ComplianceReport,
             status_code=status.HTTP_201_CREATED, tags=["Нормативний аудит"],
             summary="Перевірити версію документа і зберегти результат", responses={**_ERRORS, 404: {"description": "Версію не знайдено"}})
async def check_version(version_id: uuid.UUID, request: Request,
                        principal: Annotated[Principal, Depends(get_principal)],
                        body: CheckVersionRequest | None = None,
                        x_request_id: Annotated[str | None, Header(max_length=128)] = None) -> ComplianceReport:
    """Текст береться з `document_versions.extracted_text`. Результат пишеться в `ai_runs`
    (`compliance_check`), `compliance_checks`, `compliance_findings`, `compliance_citations`;
    якщо впевненість < 0.85 — запуск потрапляє в чергу ручної валідації оператора."""
    return await _service(request).check_version(
        principal.sub, version_id, as_of=body.as_of if body else None, request_id=x_request_id)


class SearchHit(BaseModel):
    chunk_id: uuid.UUID
    source_id: uuid.UUID
    citation_label: str
    source_title: str
    level: RegulatoryLevel
    status: RegulatoryStatus
    section_ref: str
    content: str
    is_sample: bool
    dense_score: float | None
    sparse_score: float | None
    fused_score: float
    rerank_score: float | None


class SearchResponse(BaseModel):
    coverage: Coverage
    as_of: date
    hits: list[SearchHit]


@router.get("/regulations/search", response_model=SearchResponse, tags=["Нормативний корпус"],
            summary="Гібридний пошук пунктів актів", responses=_ERRORS)
async def search(request: Request, _: Annotated[Principal, Depends(get_principal)],
                 q: Annotated[str, Query(min_length=3, max_length=2000)],
                 as_of: date | None = None, k: Annotated[int, Query(ge=1, le=30)] = 10) -> SearchResponse:
    as_of = as_of or date.today()
    res = await _service(request).search(q, as_of, k)
    return SearchResponse(coverage=res.coverage, as_of=as_of, hits=[
        SearchHit(chunk_id=c.chunk_id, source_id=c.source_id, citation_label=c.citation_label,
                  source_title=c.act.title, level=c.act.level, status=c.act.status, section_ref=c.section_ref,
                  content=c.content, is_sample=c.act.is_sample, dense_score=c.dense_score,
                  sparse_score=c.sparse_score, fused_score=c.fused_score, rerank_score=c.rerank_score)
        for c in res.clauses
    ])


class ActIn(_In):
    title: str = Field(min_length=3, max_length=1024)
    short_title: str | None = Field(None, max_length=256, examples=["Статут ПУЕТ"])
    level: RegulatoryLevel
    issuer: str = Field(min_length=2, max_length=256)
    status: RegulatoryStatus = RegulatoryStatus.IN_FORCE
    doc_number: str | None = Field(None, max_length=64)
    adopted_on: date | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    source_url: str | None = Field(None, max_length=2048)

    def to_meta(self) -> ActMeta:
        if self.status is RegulatoryStatus.REPEALED and self.effective_to is None:
            raise HTTPException(422, "Для скасованого акта потрібна дата втрати чинності (effective_to)")
        return ActMeta(title=self.title, level=self.level, issuer=self.issuer, status=self.status,
                       doc_number=self.doc_number, adopted_on=self.adopted_on, effective_from=self.effective_from,
                       effective_to=self.effective_to, short_title=self.short_title, source_url=self.source_url)


class IngestActRequest(_In):
    act: ActIn
    text: str = Field(min_length=50, max_length=3_000_000, description="Повний текст акта (UTF-8)")
    text_quality: float = Field(1.0, ge=0, le=1, description="1.0 — офіційний текст; < 1 — результат OCR")


class IngestActResponse(BaseModel):
    status: IngestStatus
    source_id: uuid.UUID
    index_revision: int
    chunks: int
    sections: list[str]


def _require_admin(request: Request, principal: Principal) -> None:
    if not (principal.roles & request.app.state.settings.corpus_admin_roles):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Змінювати нормативний корпус може лише адміністратор корпусу")


def _ingestor(request: Request) -> CorpusIngestor:
    ing: CorpusIngestor | None = getattr(request.app.state, "ingestor", None)
    if ing is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Індексацію корпусу не налаштовано")
    return ing


@router.post("/regulations", response_model=IngestActResponse, tags=["Нормативний корпус"],
             summary="Додати або переіндексувати акт", responses=_ERRORS)
async def ingest_act(body: IngestActRequest, request: Request,
                     principal: Annotated[Principal, Depends(get_principal)]) -> IngestActResponse:
    """Сегментує текст на пункти, рахує ембединги і BM25, пише ревізію в `regulatory_*` і Qdrant.
    Повторне надсилання того самого тексту нічого не змінює; змінений текст створює нову ревізію
    (старі фрагменти лишаються для історичних цитат)."""
    _require_admin(request, principal)
    actor = await request.app.state.resolve_user(principal.sub)
    rep = await _ingestor(request).ingest(body.act.to_meta(), body.text, actor_user_id=actor,
                                          text_quality=body.text_quality)
    return IngestActResponse(status=rep.status, source_id=rep.source_id, index_revision=rep.index_revision,
                             chunks=rep.chunks, sections=rep.sections)


@router.put("/regulations/{source_id}/status", status_code=status.HTTP_204_NO_CONTENT, tags=["Нормативний корпус"],
            summary="Змінити статус / строки чинності акта", responses=_ERRORS)
async def set_status(source_id: uuid.UUID, body: ActIn, request: Request,
                     principal: Annotated[Principal, Depends(get_principal)]) -> None:
    """Напр. акт скасовано: нові перевірки перестають його цитувати як чинний, а посилання на нього
    в проєктах стають зауваженням «посилання на нечинний акт»."""
    _require_admin(request, principal)
    actor = await request.app.state.resolve_user(principal.sub)
    await _ingestor(request).set_status(source_id, body.to_meta(), actor_user_id=actor)
