"""Стенд для перевірки веб-інтерфейсу в браузері: sed-app з фейками інжесту/RAG (як sed-app/tests/e2e_server.py),
справжніми PostgreSQL і модулем маршрутів, плюс модуль ключів КЕП (внутрішній ЦСК і реєстри в пам'яті) —
і зі збіркою цього фронтенду (dist/) замість вбудованого інтерфейсу.

    npm run build
    SED_TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost/sed_frontend_e2e \\
      python3 dev/stand.py --port 8766

Лише для стенду: dev-автентифікація, фейкова модель, імітаційний КНЕДП. Додатково публікує
`POST /api/v1/dev/kep/mock-key` — імітаційний файл-ключ PKCS#12 поточного користувача, щоб перевірити
закріплення КЕП через браузер (у production такого маршруту немає).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODULES = Path(os.environ.get("SED_MODULES_DIR", HERE.parent.parent))
sys.path[:0] = [str(MODULES / m) for m in ("sed-app", "db-schema", "ingest-pipeline", "rag-compliance", "approval-routing")]
sys.dont_write_bytecode = True


def main() -> None:
    import uvicorn
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import pkcs12
    from fastapi import Depends, Response
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from sed_app.auth.tokens import DevTokenIssuer
    from sed_app.config import Settings
    from sed_app.main import Components, create_app
    from sed_db.models import User
    from sed_ingest.adapters.sql_repository import SqlIngestRepository
    from sed_ingest.api.auth import get_principal
    from sed_ingest.config import Settings as IngestSettings
    from sed_ingest.pipeline.extraction import RequisitesExtractor
    from sed_ingest.pipeline.orchestrator import IngestPipeline
    from sed_ingest.pipeline.pii import RegexPseudonymizer
    from sed_routing.adapters.signature_store import InMemorySignatureStore
    from sed_routing.api.app import MockSigningDesk
    from sed_routing.config import Settings as RoutingSettings
    from sed_routing.domain.templates import TemplateCatalog
    from sed_routing.key_service import KeyManagementService
    from sed_routing.kep.challenge import ChallengeIssuer
    from sed_routing.kep.internal_ca import InternalCa, bootstrap_ca
    from sed_routing.kep.mock import MockKnedp
    from sed_routing.kep.models import PemTrustStore, StaticCertificateBinding, TaxIdHasher
    from sed_routing.kep.registry import (FirstMatchBinding, InMemoryInternalCertRegistry, InMemoryKeyRegistry,
                                          InternalRevocation, RegistryBinding)
    from sed_routing.kep.verifier import CadesVerifier, InternalTrust
    from sed_routing.service import ApprovalService

    sys.path.insert(0, str(MODULES / "sed-app"))
    from tests.conftest import FakeCompliance, MemoLlm, MemoryStorage, TextLayerOcr
    from tests.e2e_server import _prepare

    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--web-root", default=str(HERE.parent / "dist"))
    args = p.parse_args()
    url = os.environ["SED_TEST_DATABASE_URL"]
    asyncio.run(_prepare(url))

    os.environ.update(SED_ROUTING_KEP_ALLOW_MOCK="true", SED_ROUTING_ENVIRONMENT="test")
    os.environ.setdefault("SED_RAG_CORPUS_ADMIN_ROLES", '["rector","vice_rector"]')  # як у docker-compose sed-app
    settings = Settings(environment="development", auth_mode="dev", web_root=Path(args.web_root))
    sm = async_sessionmaker(create_async_engine(url), expire_on_commit=False)
    issuer = DevTokenIssuer(settings.dev_token_issuer, settings.oidc_audience, 8 * 3600)
    storage, llm = MemoryStorage(), MemoLlm()
    ing = IngestSettings()
    knedp, static, hasher = MockKnedp(), StaticCertificateBinding(), TaxIdHasher(b"x" * 32)
    keys_reg, internal_reg = InMemoryKeyRegistry(), InMemoryInternalCertRegistry()
    b = bootstrap_ca()
    ca = InternalCa(b.issuing_key, b.issuing_cert, b.root_cert, crl_url=f"http://127.0.0.1:{args.port}/api/v1/kep/internal/crl")
    verifier = CadesVerifier(
        PemTrustStore.from_pem(knedp.ca_pem), knedp, FirstMatchBinding(RegistryBinding(keys_reg), static), hasher,
        internal=InternalTrust(PemTrustStore([ca.cert]), InternalRevocation(internal_reg)), allow_mock=True)
    desk = MockSigningDesk(knedp, static, hasher, sm)
    rs = RoutingSettings()

    app = create_app(settings, components=Components(
        sessionmaker=sm, token_verifier=issuer.verifier(), dev_issuer=issuer, files=storage,
        ingest_pipeline=IngestPipeline(
            storage=storage, ocr=TextLayerOcr(), extractor=RequisitesExtractor(llm, RegexPseudonymizer(), 24_000),
            repository=SqlIngestRepository(sm, ing.document_type_codes, ing.fallback_document_type_code),
            llm_identity=llm.identity),
        compliance_service=FakeCompliance(),
        routing_service=ApprovalService(sm, TemplateCatalog.from_dir(rs.templates_dir), verifier, InMemorySignatureStore()),
        mock_desk=desk,
        kep_keys=KeyManagementService(sm, verifier, keys_reg, internal_reg, ChallengeIssuer(b"c" * 32), ca,
                                      admin_role="rector"),
    ))

    @app.post("/api/v1/dev/kep/mock-key", include_in_schema=False)
    async def mock_key(principal=Depends(get_principal)) -> Response:  # noqa: ANN001
        """Імітаційний файл-ключ КНЕДП поточного користувача (пароль `Mock-Key-2026!`)."""
        async with sm() as s:
            user = await s.scalar(select(User).where(User.keycloak_sub == principal.sub))
        assert user is not None
        k = knedp.issue(f"{user.first_name} {user.last_name.upper()}", f"{user.id.int % 10**10:010d}")
        blob = pkcs12.serialize_key_and_certificates(
            b"mock", k.private_key, k.certificate, [knedp.ca_certificate],
            serialization.PrivateFormat.PKCS12.encryption_builder().kdf_rounds(10_000)
            .key_cert_algorithm(pkcs12.PBES.PBESv2SHA256AndAES256CBC).build(b"Mock-Key-2026!"))
        return Response(blob, media_type="application/x-pkcs12")

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
