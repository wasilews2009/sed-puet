"""Фабрика FastAPI-застосунку і складання залежностей (composition root)."""

from __future__ import annotations

import json
import logging
import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from minio import Minio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sed_db.models import User
from sed_db.seeds import SYSTEM_USER_ID

from ..adapters.signature_store import InMemorySignatureStore, MinioContentReader, MinioSignatureStore
from ..adapters.sql_kep import SqlInternalCertRegistry, SqlKeyRegistry
from ..adapters.template_sync import sync_templates
from ..config import Settings
from ..domain.errors import RoutingError
from ..domain.templates import TemplateCatalog
from ..kep.challenge import ChallengeIssuer
from ..kep.external import HttpDstuVerifier
from ..kep.internal_ca import InternalCa
from ..kep.mock import MockKey, MockKnedp
from ..kep.models import CertificateBinding, PemTrustStore, StaticCertificateBinding, TaxIdHasher
from ..kep.ocsp import OcspRevocationChecker
from ..kep.registry import (
    FirstMatchBinding,
    InMemoryInternalCertRegistry,
    InMemoryKeyRegistry,
    InternalCertRegistry,
    InternalRevocation,
    KeyRegistry,
    RegistryBinding,
)
from ..kep.verifier import CadesVerifier, InternalTrust
from ..key_service import KeyManagementService
from ..ports import DocumentContentReader, SignatureStore
from ..service import ApprovalService, Caller
from .auth import KeycloakJwtVerifier, TokenVerifier
from .key_routes import router as key_router
from .routes import dev_router, router

log = logging.getLogger(__name__)


class MockSigningDesk:
    """Видає кожному користувачу імітаційний ключ і закріплює його РНОКПП-HMAC (dev/test)."""

    def __init__(self, knedp: MockKnedp, binding: StaticCertificateBinding, hasher: TaxIdHasher,
                 sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self.knedp, self.binding, self.hasher, self.sessionmaker = knedp, binding, hasher, sessionmaker
        self._keys: dict[uuid.UUID, MockKey] = {}

    async def key_for(self, caller: Caller) -> MockKey:
        async with self.sessionmaker() as s:
            user = await s.scalar(select(User).where(User.keycloak_sub == caller.keycloak_sub))
        if user is None:
            raise RoutingError("Користувача не знайдено")
        if user.id not in self._keys:
            tax_id = f"{user.id.int % 10**10:010d}"  # умовний «РНОКПП» для імітації
            self._keys[user.id] = self.knedp.issue(f"{user.first_name} {user.last_name.upper()}", tax_id)
            self.binding.bind(user.id, self.hasher(tax_id))
        return self._keys[user.id]


def create_app(
    settings: Settings | None = None,
    *,
    service: ApprovalService | None = None,
    token_verifier: TokenVerifier | None = None,
    mock_desk: MockSigningDesk | None = None,
    keys: KeyManagementService | None = None,
) -> FastAPI:
    """`service` / `keys` / `token_verifier` / `mock_desk` передаються в тестах; у продакшні
    збираються з `settings`."""
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.token_verifier = token_verifier or KeycloakJwtVerifier(
            settings.oidc_issuer, settings.oidc_audience, settings.oidc_jwks_url)
        app.state.mock_desk = mock_desk
        if service is not None:
            app.state.service, app.state.keys = service, keys
            yield
            return

        engine = create_async_engine(settings.database_url, pool_pre_ping=True)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        catalog = TemplateCatalog.from_dir(settings.templates_dir)
        if settings.sync_templates_on_startup:
            async with sessionmaker() as s, s.begin():
                for r in await sync_templates(s, catalog, SYSTEM_USER_ID):
                    (log.error if r.error else log.info)("route template %s v%s: %s %s",
                                                         r.code, r.version, r.action, r.error or "")
        hasher = TaxIdHasher(settings.kep_tax_id_hmac_secret.get_secret_value().encode())
        static = StaticCertificateBinding()
        if settings.kep_bindings_file and settings.kep_bindings_file.exists():
            for uid, h in json.loads(settings.kep_bindings_file.read_text()).items():
                static.bind(uuid.UUID(uid), h)
        key_registry: KeyRegistry
        internal_registry: InternalCertRegistry
        if settings.kep_registry_sql:
            key_registry, internal_registry = SqlKeyRegistry(sessionmaker), SqlInternalCertRegistry(sessionmaker)
        else:
            key_registry, internal_registry = InMemoryKeyRegistry(), InMemoryInternalCertRegistry()
        binding: CertificateBinding = FirstMatchBinding(RegistryBinding(key_registry), static)
        anchors = (PemTrustStore.from_pem(settings.kep_trust_anchors_pem.read_bytes()).anchors
                   if settings.kep_trust_anchors_pem.exists() else [])
        ca: InternalCa | None = None
        internal: InternalTrust | None = None
        if settings.internal_ca_enabled:
            pwd = settings.internal_ca_key_password.get_secret_value().encode() or None
            ca = InternalCa.from_pem(settings.internal_ca_key_pem.read_bytes(), pwd,
                                     settings.internal_ca_chain_pem.read_bytes(),
                                     cert_validity_days=settings.internal_ca_validity_days,
                                     crl_url=f"{settings.internal_ca_public_url}/crl")
            internal = InternalTrust(PemTrustStore([ca.cert]), InternalRevocation(internal_registry))
        challenge_secret = settings.kep_challenge_secret.get_secret_value().encode()
        if not challenge_secret and settings.environment in {"development", "test"}:
            challenge_secret = secrets.token_bytes(32)  # виклики живуть до перезапуску — у dev цього досить
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0)) as http:
            revocation = OcspRevocationChecker(http)
            dstu = HttpDstuVerifier(http, settings.kep_dstu_verifier_url) if settings.kep_dstu_verifier_url else None
            store: SignatureStore
            reader: DocumentContentReader | None = None
            if settings.kep_allow_mock:
                knedp = MockKnedp()
                anchors.append(knedp.ca_certificate)
                revocation = knedp  # type: ignore[assignment]
                store = InMemorySignatureStore(settings.minio_signatures_bucket)
                app.state.mock_desk = MockSigningDesk(knedp, static, hasher, sessionmaker)
                log.warning("Увімкнено імітаційний КНЕДП: підписи не мають юридичної сили")
            else:
                minio = Minio(settings.minio_endpoint, access_key=settings.minio_access_key.get_secret_value(),
                              secret_key=settings.minio_secret_key.get_secret_value(), secure=settings.minio_secure)
                store = MinioSignatureStore(minio, settings.minio_signatures_bucket, sse=settings.minio_sse)
                reader = MinioContentReader(minio)
            verifier = CadesVerifier(
                PemTrustStore(anchors), revocation, binding, hasher, internal=internal, dstu=dstu,
                allow_mock=settings.kep_allow_mock, require_qualified=settings.kep_require_qualified,
                max_container_bytes=settings.kep_max_container_bytes)
            app.state.service = ApprovalService(
                sessionmaker, catalog, verifier, store,
                block_on_critical_findings=settings.block_on_critical_findings,
                ai_service_roles=settings.ai_service_roles, content_reader=reader)
            app.state.keys = KeyManagementService(
                sessionmaker, verifier, key_registry, internal_registry, ChallengeIssuer(challenge_secret), ca,
                admin_role=settings.kep_admin_role, require_name_match=settings.kep_require_name_match)
            try:
                yield
            finally:
                await engine.dispose()

    app = FastAPI(
        title="СЕД ПУЕТ — маршрути погодження",
        version="0.1.0",
        description="Визначення учасників погодження за шаблоном, послідовне візування з КЕП; ключі підпису "
                    "(КЕП Дії, ПриватБанку, інших КНЕДП) і внутрішні ключі АІ-СЕД ПУЕТ.",
        lifespan=lifespan,
    )

    @app.exception_handler(RoutingError)
    async def _routing_error(_: Request, exc: RoutingError) -> JSONResponse:
        if exc.http_status >= 500:
            log.error("routing failed: %s (%s)", exc.code, exc.message)
        return JSONResponse({"detail": exc.message, "code": exc.code, **exc.details}, status_code=exc.http_status)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(router)
    app.include_router(key_router)
    if settings.kep_allow_mock or mock_desk is not None:
        app.include_router(dev_router)
    return app
