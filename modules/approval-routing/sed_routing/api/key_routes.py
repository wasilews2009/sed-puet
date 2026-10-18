"""HTTP API ключів підпису: закріплення КЕП (Дія, ПриватБанк, інші КНЕДП) і внутрішні ключі АІ-СЕД ПУЕТ."""

from __future__ import annotations

import uuid
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, UploadFile, status
from fastapi.responses import Response

from .. import views
from ..domain.errors import RoutingError
from ..key_service import KeyManagementService
from ..service import Caller
from .auth import get_caller

router = APIRouter(prefix="/api/v1/kep", tags=["Ключі підпису"])

CallerDep = Annotated[Caller, Depends(get_caller)]
MAX_CERT_BYTES = 64 * 1024


def get_keys(request: Request) -> KeyManagementService:
    return request.app.state.keys


KeysDep = Annotated[KeyManagementService, Depends(get_keys)]

_ERR = {
    401: {"description": "Немає або недійсний токен Keycloak"},
    403: {"description": "Не власник ключа і не адміністратор ключів"},
    404: {"description": "Ключ не знайдено або внутрішній ЦСК вимкнено"},
    422: {"description": "Підпис або сертифікат не пройшли перевірку; надіслано файл з особистим ключем"},
}


@router.post("/challenges", response_model=views.ChallengeView, responses=_ERR,
             summary="Отримати виклик для доказу володіння ключем КЕП")
async def challenge(caller: CallerDep, keys: KeysDep) -> views.ChallengeView:
    return await keys.challenge(caller)


@router.post("/keys", response_model=views.KeyBindingView, responses=_ERR, status_code=status.HTTP_201_CREATED,
             summary="Закріпити за собою ключ КЕП (підпис виклику; файл-ключ на сервер не передається)")
async def register_key(body: views.KeyRegisterRequest, caller: CallerDep, keys: KeysDep) -> views.KeyBindingView:
    return await keys.register(caller, body)


@router.get("/keys", response_model=list[views.KeyBindingView], responses=_ERR, summary="Мої ключі підпису")
async def my_keys(caller: CallerDep, keys: KeysDep, include_revoked: bool = False) -> list[views.KeyBindingView]:
    return await keys.my_keys(caller, include_revoked=include_revoked)


@router.post("/keys/{binding_id}/revoke", response_model=views.KeyBindingView, responses=_ERR,
             summary="Відкріпити ключ (власник або адміністратор ключів)")
async def revoke_key(binding_id: uuid.UUID, body: views.KeyRevokeRequest, caller: CallerDep,
                     keys: KeysDep) -> views.KeyBindingView:
    return await keys.revoke_key(caller, binding_id, body)


@router.post("/certificates/inspect", response_model=views.CertificateCheckView, responses=_ERR,
             summary="Перевірити сертифікат (.cer/.crt/.pem): власник, КНЕДП, строк, РНОКПП (маскований)")
async def inspect_certificate(file: UploadFile, caller: CallerDep, keys: KeysDep) -> views.CertificateCheckView:
    data = await file.read(MAX_CERT_BYTES + 1)
    if len(data) > MAX_CERT_BYTES:
        raise RoutingError("Файл завеликий для сертифіката")
    return await keys.inspect_certificate(caller, data)


@router.post("/signatures/verify", response_model=views.SignatureVerifyView, responses=_ERR,
             summary="Перевірити підпис CAdES над SHA-256 (без прийняття візи)")
async def verify_signature(body: views.SignatureVerifyRequest, caller: CallerDep,
                           keys: KeysDep) -> views.SignatureVerifyView:
    return await keys.verify_signature(caller, body)


@router.get("/internal/ca.pem", responses={200: {"content": {"application/x-pem-file": {}}}},
            response_class=Response, summary="Ланцюжок сертифікатів внутрішнього ЦСК АІ-СЕД ПУЕТ")
async def ca_chain(keys: KeysDep) -> Response:
    return Response(keys.ca_chain_pem(), media_type="application/x-pem-file")


@router.get("/internal/crl", responses={200: {"content": {"application/pkix-crl": {}}}},
            response_class=Response, summary="Список відкликаних внутрішніх сертифікатів (CRL, DER)")
async def crl(keys: KeysDep) -> Response:
    return Response(await keys.crl(), media_type="application/pkix-crl")


@router.post("/internal/certificates/csr", response_model=views.InternalCertView, responses=_ERR,
             status_code=status.HTTP_201_CREATED,
             summary="Отримати внутрішній сертифікат за CSR (ключ згенеровано на вашому комп'ютері)")
async def enroll_csr(body: views.CsrEnrollRequest, caller: CallerDep, keys: KeysDep) -> views.InternalCertView:
    return await keys.enroll_csr(caller, body)


@router.post("/internal/key-file", responses={**_ERR, 201: {"content": {"application/x-pkcs12": {}}}},
             response_class=Response, status_code=status.HTTP_201_CREATED,
             summary="Згенерувати внутрішній файл-ключ (PKCS#12, видається один раз)")
async def key_file(body: views.KeyFileRequest, caller: CallerDep, keys: KeysDep) -> Response:
    blob, name = await keys.issue_key_file(caller, body)
    return Response(blob, status_code=status.HTTP_201_CREATED, media_type="application/x-pkcs12",
                    headers={"Content-Disposition": f'attachment; filename="{name.encode("ascii", "replace").decode()}"; '
                                                    f"filename*=UTF-8''{quote(name, safe='')}",
                             "Cache-Control": "no-store"})


@router.post("/internal/certificates/{serial}/revoke", status_code=status.HTTP_204_NO_CONTENT, responses=_ERR,
             summary="Відкликати внутрішній сертифікат (власник або адміністратор ключів)")
async def revoke_internal(serial: str, body: views.KeyRevokeRequest, caller: CallerDep, keys: KeysDep) -> Response:
    await keys.revoke_internal(caller, serial, body)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
