"""Фабрика FastAPI-застосунку і складання залежностей (composition root)."""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sed_ingest.adapters.openai_compat_llm import OpenAICompatibleLlm
from sed_ingest.api.auth import KeycloakJwtVerifier, TokenVerifier
from sed_ingest.domain.models import LlmIdentity
from sed_ingest.pipeline.pii import RegexPseudonymizer

from ..adapters.embeddings import OpenAICompatibleEmbedder, TeiReranker
from ..adapters.qdrant_index import QdrantVectorIndex
from ..adapters.sql_compliance import SqlComplianceRepository
from ..adapters.sql_corpus import SqlCorpusRepository
from ..audit.graph import ComplianceAuditor
from ..config import Settings
from ..corpus.sparse import Bm25Encoder
from ..domain.models import RagError
from ..ingestion.service import CorpusIngestor
from ..retrieval.hybrid import HybridRetriever
from ..service import ComplianceService
from .routes import router

log = logging.getLogger(__name__)


@asynccontextmanager
async def build_components(settings: Settings) -> AsyncIterator[dict[str, object]]:
    """Усі адаптери продакшн-конфігурації; закриваються при виході з контексту."""
    async with AsyncExitStack() as stack:
        llm_http = await stack.enter_async_context(httpx.AsyncClient(
            base_url=settings.llm_base_url,
            headers={"Authorization": f"Bearer {settings.llm_api_key.get_secret_value()}"},
            timeout=httpx.Timeout(settings.llm_timeout_s, connect=5.0)))
        emb_http = await stack.enter_async_context(httpx.AsyncClient(
            base_url=settings.embedding_base_url,
            headers={"Authorization": f"Bearer {settings.embedding_api_key.get_secret_value()}"},
            timeout=httpx.Timeout(60.0, connect=5.0)))
        reranker = None
        if settings.reranker_url:
            rr_http = await stack.enter_async_context(httpx.AsyncClient(base_url=settings.reranker_url,
                                                                        timeout=httpx.Timeout(30.0, connect=5.0)))
            reranker = TeiReranker(rr_http)
        qdrant = AsyncQdrantClient(
            url=settings.qdrant_url, timeout=settings.qdrant_timeout_s,
            api_key=settings.qdrant_api_key.get_secret_value() if settings.qdrant_api_key else None)
        stack.push_async_callback(qdrant.close)
        engine = create_async_engine(settings.database_url, pool_pre_ping=True)
        stack.push_async_callback(engine.dispose)
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        index = QdrantVectorIndex(qdrant, settings.qdrant_collection)
        embedder = OpenAICompatibleEmbedder(
            emb_http, settings.embedding_model, settings.embedding_model_version, settings.embedding_dim,
            query_prefix=settings.embedding_query_prefix, passage_prefix=settings.embedding_passage_prefix,
            batch_size=settings.embedding_batch_size)
        bm25 = Bm25Encoder()
        masker = RegexPseudonymizer()
        identity = LlmIdentity(settings.llm_model, settings.llm_model_version, settings.llm_provider)
        llm = OpenAICompatibleLlm(llm_http, settings.llm_model, identity,
                                  max_tokens=settings.llm_max_output_tokens, seed=settings.llm_seed)
        retriever = HybridRetriever(index, embedder, bm25, settings.retrieval, reranker)
        auditor = ComplianceAuditor(retriever=retriever, index=index, llm=llm, masker=masker, settings=settings)
        corpus_repo = SqlCorpusRepository(sessions)
        yield {
            "service": ComplianceService(auditor, retriever, SqlComplianceRepository(sessions, masker)),
            "ingestor": CorpusIngestor(repository=corpus_repo, index=index, embedder=embedder, bm25=bm25),
            "resolve_user": corpus_repo.resolve_user,
            "index": index,
            "embedder": embedder,
        }


def create_app(
    settings: Settings | None = None,
    *,
    service: ComplianceService | None = None,
    ingestor: CorpusIngestor | None = None,
    resolve_user: Callable[[uuid.UUID], Awaitable[uuid.UUID]] | None = None,
    token_verifier: TokenVerifier | None = None,
) -> FastAPI:
    """`service` / `ingestor` / `token_verifier` передаються в тестах; у продакшні збираються з `settings`."""
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.token_verifier = token_verifier or KeycloakJwtVerifier(
            settings.oidc_issuer, settings.oidc_audience, settings.oidc_jwks_url)
        if service is not None:
            app.state.service, app.state.ingestor, app.state.resolve_user = service, ingestor, resolve_user
            yield
            return
        async with build_components(settings) as c:
            app.state.service, app.state.ingestor, app.state.resolve_user = c["service"], c["ingestor"], c["resolve_user"]
            try:
                await c["index"].ensure_collection(settings.embedding_dim)  # type: ignore[attr-defined]
            except RagError as exc:     # Qdrant ще не піднявся — застосунок стартує, /readyz покаже стан
                log.error("Qdrant collection check failed: %s", exc.message)
            yield

    app = FastAPI(
        title="СЕД ПУЕТ — нормативний аудит (RAG)",
        version="0.1.0",
        description=("Перевірка проєктів наказів і розпоряджень на відповідність Статуту ПУЕТ, положенням "
                     "університету, законам України і наказам МОН: гібридний пошук у Qdrant, граф LangGraph, "
                     "локальна LLM. Звіт ШІ не має юридичної сили: рішення приймає людина, що накладає КЕП."),
        lifespan=lifespan,
    )

    @app.exception_handler(RagError)
    async def _rag_error(_: Request, exc: RagError) -> JSONResponse:
        if exc.http_status >= 500:
            log.error("compliance failed: %s (%s)", exc.code, exc.message)
        return JSONResponse({"detail": exc.message, "code": exc.code}, status_code=exc.http_status)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(router)
    return app
