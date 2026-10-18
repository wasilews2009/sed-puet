"""Фабрика FastAPI-застосунку і складання залежностей (composition root)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from minio import Minio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ..adapters.minio_storage import MinioObjectStorage
from ..adapters.ocr_engine import TesseractOcrEngine
from ..adapters.openai_compat_llm import OpenAICompatibleLlm
from ..adapters.sql_repository import SqlIngestRepository
from ..config import Settings
from ..domain.models import IngestError, LlmIdentity
from ..pipeline.extraction import RequisitesExtractor
from ..pipeline.orchestrator import IngestPipeline
from ..pipeline.pii import RegexPseudonymizer
from .auth import KeycloakJwtVerifier, TokenVerifier
from .routes import router

log = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    pipeline: IngestPipeline | None = None,
    token_verifier: TokenVerifier | None = None,
) -> FastAPI:
    """`pipeline` / `token_verifier` передаються в тестах; у продакшні збираються з `settings`."""
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.token_verifier = token_verifier or KeycloakJwtVerifier(
            settings.oidc_issuer, settings.oidc_audience, settings.oidc_jwks_url)
        if pipeline is not None:
            app.state.pipeline = pipeline
            yield
            return
        async with httpx.AsyncClient(
            base_url=settings.llm_base_url,
            headers={"Authorization": f"Bearer {settings.llm_api_key.get_secret_value()}"},
            timeout=httpx.Timeout(settings.llm_timeout_s, connect=5.0),
        ) as llm_http:
            engine = create_async_engine(settings.database_url, pool_pre_ping=True)
            storage = MinioObjectStorage(
                Minio(settings.minio_endpoint, access_key=settings.minio_access_key.get_secret_value(),
                      secret_key=settings.minio_secret_key.get_secret_value(), secure=settings.minio_secure),
                settings.minio_bucket, sse=settings.minio_sse,
            )
            storage.ensure_bucket()
            identity = LlmIdentity(settings.llm_model, settings.llm_model_version, settings.llm_provider)
            llm = OpenAICompatibleLlm(llm_http, settings.llm_model, identity,
                                      max_tokens=settings.llm_max_output_tokens, seed=settings.llm_seed)
            app.state.pipeline = IngestPipeline(
                storage=storage,
                ocr=TesseractOcrEngine(
                    lang=settings.tesseract_lang, dpi=settings.ocr_dpi, max_pages=settings.max_pages,
                    page_timeout_s=settings.ocr_page_timeout_s,
                    text_layer_min_chars=settings.text_layer_min_chars,
                    max_image_pixels=settings.max_image_pixels,
                ),
                extractor=RequisitesExtractor(llm, RegexPseudonymizer(), settings.llm_max_input_chars),
                repository=SqlIngestRepository(
                    async_sessionmaker(engine, expire_on_commit=False),
                    settings.document_type_codes, settings.fallback_document_type_code,
                ),
                llm_identity=identity,
            )
            try:
                yield
            finally:
                await engine.dispose()

    app = FastAPI(
        title="СЕД ПУЕТ — інжест документів",
        version="0.1.0",
        description="Прийом PDF/зображень, OCR і вилучення реквізитів за ДСТУ 4163:2020 локальною LLM.",
        lifespan=lifespan,
    )

    @app.exception_handler(IngestError)
    async def _ingest_error(_: Request, exc: IngestError) -> JSONResponse:
        if exc.http_status >= 500:
            log.error("ingest failed: %s (%s)", exc.code, exc.message)
        return JSONResponse({"detail": exc.message, "code": exc.code}, status_code=exc.http_status)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(router)
    return app
