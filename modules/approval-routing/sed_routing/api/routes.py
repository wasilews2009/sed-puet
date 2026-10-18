"""HTTP API маршрутизації погодження."""

from __future__ import annotations

import base64
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from sed_db.schemas import StepDecision

from .. import views
from ..service import ApprovalService, Caller
from .auth import get_caller

router = APIRouter(prefix="/api/v1", tags=["Маршрути погодження"])
dev_router = APIRouter(prefix="/api/v1/dev", tags=["Імітація КЕП (лише development/test)"])

CallerDep = Annotated[Caller, Depends(get_caller)]


def get_service(request: Request) -> ApprovalService:
    return request.app.state.service


ServiceDep = Annotated[ApprovalService, Depends(get_service)]

_ERRORS = {
    401: {"description": "Немає або недійсний токен Keycloak"},
    403: {"description": "Немає права на цю дію (не автор/ініціатор/учасник кроку, сервісний акаунт ШІ)"},
    404: {"description": "Документ, маршрут або крок не знайдено"},
    409: {"description": "Неприпустимо в поточному стані: етап ще не настав, рішення вже прийнято, "
                         "посада вакантна, немає/кілька шаблонів, відкриті критичні зауваження"},
}


@router.post("/documents/{document_id}/route-preview", response_model=views.RoutePlanView, responses=_ERRORS,
             summary="Визначити посадових осіб, які мають завізувати документ (без збереження)")
async def preview(document_id: uuid.UUID, body: views.RouteRequest, caller: CallerDep,
                  service: ServiceDep) -> views.RoutePlanView:
    return await service.preview(caller, document_id, body)


@router.post("/documents/{document_id}/routes", response_model=views.RouteView, responses=_ERRORS,
             status_code=status.HTTP_201_CREATED,
             summary="Створити маршрут погодження за шаблоном і (за замовчуванням) запустити його")
async def create_route(document_id: uuid.UUID, body: views.RouteCreate, caller: CallerDep,
                       service: ServiceDep) -> views.RouteView:
    return await service.create_route(caller, document_id, body)


@router.get("/documents/{document_id}/route", response_model=views.RouteView, responses=_ERRORS,
            summary="Останній маршрут документа")
async def document_route(document_id: uuid.UUID, caller: CallerDep, service: ServiceDep) -> views.RouteView:
    return await service.get_route(caller, await service.current_route_id(caller, document_id))


@router.get("/routes/{route_id}", response_model=views.RouteView, responses=_ERRORS, summary="Стан маршруту")
async def get_route(route_id: uuid.UUID, caller: CallerDep, service: ServiceDep) -> views.RouteView:
    return await service.get_route(caller, route_id)


@router.post("/routes/{route_id}/start", response_model=views.RouteView, responses=_ERRORS,
             summary="Запустити створений маршрут")
async def start_route(route_id: uuid.UUID, body: views.RouteStart, caller: CallerDep,
                      service: ServiceDep) -> views.RouteView:
    return await service.start_route(caller, route_id, body)


@router.post("/routes/{route_id}/cancel", response_model=views.RouteView, responses=_ERRORS,
             summary="Скасувати маршрут (ініціатор або автор)")
async def cancel_route(route_id: uuid.UUID, body: views.RouteCancel, caller: CallerDep,
                       service: ServiceDep) -> views.RouteView:
    return await service.cancel_route(caller, route_id, body)


@router.get("/approval-tasks", response_model=list[views.InboxItem], responses=_ERRORS,
            summary="Мої документи на візування (активні кроки)")
async def inbox(caller: CallerDep, service: ServiceDep) -> list[views.InboxItem]:
    return await service.inbox(caller)


@router.get("/steps/{step_id}/signing-request", response_model=views.SigningRequestView, responses=_ERRORS,
            summary="Що підписувати: версія документа і її SHA-256")
async def signing_request(step_id: uuid.UUID, caller: CallerDep, service: ServiceDep) -> views.SigningRequestView:
    return await service.signing_request(caller, step_id)


@router.post("/steps/{step_id}/sign", response_model=views.RouteView,
             responses={**_ERRORS, 422: {"description": "Підпис КЕП не пройшов перевірку (причина в detail)"}},
             summary="Завізувати крок із КЕП; наступний етап активується лише після валідного підпису")
async def sign_step(step_id: uuid.UUID, body: views.SignRequest, caller: CallerDep,
                    service: ServiceDep) -> views.RouteView:
    return await service.sign(caller, step_id, body)


@router.post("/steps/{step_id}/decision", response_model=views.RouteView, responses=_ERRORS,
             summary="Відхилити крок (з причиною) або погодити крок, що не потребує КЕП")
async def decide_step(step_id: uuid.UUID, body: StepDecision, caller: CallerDep,
                      service: ServiceDep) -> views.RouteView:
    return await service.decide(caller, step_id, body)


class MockSignature(BaseModel):
    signature_base64: str
    certificate_subject: str
    warning: str = "Імітація КЕП: не має юридичної сили"


@dev_router.post("/steps/{step_id}/mock-signature", response_model=MockSignature,
                 summary="Згенерувати імітаційний КЕП поточного користувача для кроку")
async def mock_signature(step_id: uuid.UUID, request: Request, caller: CallerDep,
                         service: ServiceDep) -> MockSignature:
    desk = getattr(request.app.state, "mock_desk", None)
    if desk is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Імітацію КЕП вимкнено")
    req = await service.signing_request(caller, step_id)
    key = await desk.key_for(caller)
    return MockSignature(signature_base64=base64.b64encode(key.sign(req.content_sha256)).decode(),
                         certificate_subject=key.certificate.subject.rfc4514_string())
