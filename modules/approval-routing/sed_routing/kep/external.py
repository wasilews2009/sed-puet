"""Перевірка підписів ДСТУ 4145 через сертифікований криптопровайдер (окремий сервіс).

КЕП від Дії, ПриватБанку та більшості українських КНЕДП використовує ДСТУ 4145 з гешем
ГОСТ 34.311 / ДСТУ 7564. Ці алгоритми має виконувати сертифікований засіб КЕП (бібліотека
ІІТ «EUSignCP» або SDK Дія.Підпис). Ліцензійні бібліотеки розгортаються окремим сервісом
(«sidecar») у внутрішній мережі, а СЕД звертається до нього за контрактом нижче. Політику
(чий ключ, чи кваліфікований, чи не раніше надходження документа) застосовує наш верифікатор.

Контракт sidecar (див. README, розділ «Файлові ключі»):

    POST {base_url}/v1/verify-detached
    {"signature_base64": "...", "data_base64": "..."}
    →
    {"valid": true, "reasons": [],
     "signer_certificate_base64": "...",         # DER сертифіката підписувача
     "signing_time": "2026-10-17T19:00:00Z",      # з атрибута signing-time
     "timestamp_time": "2026-10-17T19:00:02Z",    # з позначки часу (CAdES-T/X-Long), якщо є
     "revocation": "good" | "revoked" | "unknown",
     "chain_valid": true,                         # ланцюжок до сертифіката КНЕДП із довірчого списку
     "signature_format": "CAdES-BES" | "CAdES-X-Long"}
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

import httpx

from .models import RevocationStatus


@dataclass(frozen=True)
class ExternalVerification:
    valid: bool
    reasons: tuple[str, ...] = ()
    signer_certificate_der: bytes | None = None
    signing_time: datetime | None = None
    timestamp_time: datetime | None = None
    revocation: RevocationStatus = RevocationStatus.UNKNOWN
    chain_valid: bool = False
    signature_format: str | None = None
    raw: dict = field(default_factory=dict)


class ExternalVerifierUnavailable(Exception):
    pass


class DstuVerifier(Protocol):
    async def verify_detached(self, container: bytes, data: bytes) -> ExternalVerification: ...


class HttpDstuVerifier:
    def __init__(self, client: httpx.AsyncClient, base_url: str) -> None:
        self.client, self.base_url = client, base_url.rstrip("/")

    async def verify_detached(self, container: bytes, data: bytes) -> ExternalVerification:
        try:
            r = await self.client.post(f"{self.base_url}/v1/verify-detached", json={
                "signature_base64": base64.b64encode(container).decode(),
                "data_base64": base64.b64encode(data).decode()})
            r.raise_for_status()
            j = r.json()
            return ExternalVerification(
                valid=bool(j["valid"]), reasons=tuple(j.get("reasons") or ()),
                signer_certificate_der=base64.b64decode(j["signer_certificate_base64"])
                if j.get("signer_certificate_base64") else None,
                signing_time=datetime.fromisoformat(j["signing_time"]) if j.get("signing_time") else None,
                timestamp_time=datetime.fromisoformat(j["timestamp_time"]) if j.get("timestamp_time") else None,
                revocation=RevocationStatus(j.get("revocation", "unknown")),
                chain_valid=bool(j.get("chain_valid")), signature_format=j.get("signature_format"), raw=j)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise ExternalVerifierUnavailable(str(exc)) from exc
