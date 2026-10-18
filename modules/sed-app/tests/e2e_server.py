"""Сервер для браузерної перевірки веб-інтерфейсу без MinIO/Tesseract/LLM: ті самі фейки, що в conftest
(сховище в пам'яті, «модель», що читає текст, імітаційний КНЕДП), справжні PostgreSQL і модулі.

    SED_TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost/sed_app_e2e \
    PYTHONPATH=.:../db-schema:../ingest-pipeline:../rag-compliance:../approval-routing \
    python3 -m tests.e2e_server --port 8765

Не для production: dev-автентифікація і фейкова модель.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.dont_write_bytecode = True


async def _prepare(url: str) -> None:
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from sed_app.seeds.demo import load, sync_route_templates
    from sed_db.seeds import SYSTEM_USER_ID

    from .conftest import MODULES

    u = make_url(url)
    admin = create_engine(u.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{u.database}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{u.database}"'))
    admin.dispose()
    os.environ["SED_DATABASE_URL"] = url
    command.upgrade(Config(str(MODULES / "db-schema" / "alembic.ini")), "head")
    eng = create_async_engine(url)
    async with async_sessionmaker(eng, expire_on_commit=False)() as s, s.begin():
        await s.execute(text("SELECT set_config('sed.actor_user_id', :u, true)"), {"u": str(SYSTEM_USER_ID)})
        await load(s)
        await sync_route_templates(s)
    await eng.dispose()


def main() -> None:
    import uvicorn
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from sed_app.auth.tokens import DevTokenIssuer
    from sed_app.config import Settings
    from sed_app.main import Components, create_app
    from sed_ingest.adapters.sql_repository import SqlIngestRepository
    from sed_ingest.config import Settings as IngestSettings
    from sed_ingest.pipeline.extraction import RequisitesExtractor
    from sed_ingest.pipeline.orchestrator import IngestPipeline
    from sed_ingest.pipeline.pii import RegexPseudonymizer
    from sed_routing.adapters.signature_store import InMemorySignatureStore
    from sed_routing.api.app import MockSigningDesk
    from sed_routing.config import Settings as RoutingSettings
    from sed_routing.domain.templates import TemplateCatalog
    from sed_routing.kep.mock import MockKnedp
    from sed_routing.kep.models import PemTrustStore, StaticCertificateBinding, TaxIdHasher
    from sed_routing.kep.verifier import CadesVerifier

    from .conftest import FakeCompliance, MemoLlm, MemoryStorage, TextLayerOcr

    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8765)
    args = p.parse_args()
    url = os.environ["SED_TEST_DATABASE_URL"]
    asyncio.run(_prepare(url))

    os.environ.update(SED_ROUTING_KEP_ALLOW_MOCK="true", SED_ROUTING_ENVIRONMENT="test")
    settings = Settings(environment="development", auth_mode="dev")
    sm = async_sessionmaker(create_async_engine(url), expire_on_commit=False)
    issuer = DevTokenIssuer(settings.dev_token_issuer, settings.oidc_audience, 8 * 3600)
    storage, llm = MemoryStorage(), MemoLlm()
    ing = IngestSettings()
    knedp, binding, hasher = MockKnedp(), StaticCertificateBinding(), TaxIdHasher(b"x" * 32)
    from sed_routing.service import ApprovalService

    app = create_app(settings, components=Components(
        sessionmaker=sm, token_verifier=issuer.verifier(), dev_issuer=issuer, files=storage,
        ingest_pipeline=IngestPipeline(
            storage=storage, ocr=TextLayerOcr(), extractor=RequisitesExtractor(llm, RegexPseudonymizer(), 24_000),
            repository=SqlIngestRepository(sm, ing.document_type_codes, ing.fallback_document_type_code),
            llm_identity=llm.identity),
        compliance_service=FakeCompliance(),
        routing_service=ApprovalService(
            sm, TemplateCatalog.from_dir(RoutingSettings().templates_dir),
            CadesVerifier(PemTrustStore.from_pem(knedp.ca_pem), knedp, binding, hasher, allow_mock=True),
            InMemorySignatureStore()),
        mock_desk=MockSigningDesk(knedp, binding, hasher, sm),
    ))
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
