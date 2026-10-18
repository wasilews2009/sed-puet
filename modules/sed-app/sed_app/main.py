"""Composition root: один FastAPI-застосунок над модулями СЕД.

```
                         ┌──────────────── sed_app (цей пакет) ────────────────┐
 браузер ── /  ─────────▶│ веб-інтерфейс (static SPA)                          │
         ── /api/v1 ────▶│ auth/me, довідники, список/картка, HITL, черги      │
                         │   ├── POST /documents/ingest      → sed_ingest       │
                         │   ├── /compliance/*, /regulations → sed_rag          │
                         │   └── /routes, /steps, /approval-tasks → sed_routing │
                         └──────┬───────────┬───────────┬─────────────┬────────┘
                           PostgreSQL     MinIO       Qdrant     vLLM/Ollama, TEI
```

Модулі вмикаються незалежно (`SED_APP_ENABLE_*`): кожен збирається своєю штатною фабрикою
(`create_app` / `build_components`), застосунок лише входить у їхній lifespan і підставляє
спільну автентифікацію. Код модулів не змінюється.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sed_ingest.api import routes as ingest_routes
from sed_ingest.api.auth import TokenVerifier, get_principal
from sed_ingest.config import Settings as IngestSettings
from sed_ingest.domain.models import IngestError

from . import __version__
from .adapters.file_storage import FileReader, MinioFileReader
from .adapters.sql_documents import SqlDocumentReader
from .adapters.sql_review import SqlReviewRepository
from .adapters.sql_users import SqlUserDirectory
from .api import routes_auth, routes_documents
from .api.deps import AppServices, require
from .auth.tokens import AppPrincipal, DevTokenIssuer, OidcVerifier
from .config import AuthMode, Settings
from .domain.errors import AppError

log = logging.getLogger(__name__)

# Шляхи модулів, які застосунок не публікує: їх замінено власними ендпоінтами з єдиною політикою доступу.
_HIDDEN_MODULE_PATHS = {
    # sed_rag: власна перевірка доступу ширша за політику застосунку (див. README → «Що потрібно від модулів»)
    "/api/v1/documents/versions/{version_id}/compliance-checks",
}


@dataclass
class Components:
    """Готові залежності (тести, вбудовування). Порожнє поле — модуль вимкнено."""

    sessionmaker: async_sessionmaker[AsyncSession]
    token_verifier: TokenVerifier
    dev_issuer: DevTokenIssuer | None = None
    files: FileReader | None = None
    ingest_pipeline: Any = None
    compliance_service: Any = None
    corpus_ingestor: Any = None
    resolve_user: Callable[[uuid.UUID], Awaitable[uuid.UUID]] | None = None
    routing_service: Any = None
    mock_desk: Any = None
    kep_keys: Any = None


def _optional_modules() -> tuple[Any, Any]:
    """Модулі RAG і маршрутів можуть бути не встановлені на стенді (окремі залежності)."""
    try:
        from sed_rag.api import app as rag_app
    except ImportError as exc:  # pragma: no cover — залежить від оточення
        log.warning("sed_rag недоступний: %s", exc)
        rag_app = None
    try:
        from sed_routing.api import app as routing_app
    except ImportError as exc:  # pragma: no cover
        log.warning("sed_routing недоступний: %s", exc)
        routing_app = None
    return rag_app, routing_app


@asynccontextmanager
async def _build(settings: Settings, ingest_settings: IngestSettings, rag_settings: Any,
                 routing_settings: Any, rag_mod: Any, routing_mod: Any) -> AsyncIterator[Components]:
    async with AsyncExitStack() as stack:
        engine = create_async_engine(settings.database_url, pool_pre_ping=True, pool_size=settings.db_pool_size)
        stack.push_async_callback(engine.dispose)
        sm = async_sessionmaker(engine, expire_on_commit=False)

        dev_issuer = None
        if settings.auth_mode is AuthMode.DEV:
            dev_issuer = DevTokenIssuer(
                settings.dev_token_issuer, settings.oidc_audience, settings.dev_token_ttl_s,
                settings.dev_signing_key_pem.get_secret_value() if settings.dev_signing_key_pem else None)
            verifier: TokenVerifier = dev_issuer.verifier()
            log.warning("Увімкнено dev-автентифікацію: вхід демо-користувачем без пароля")
        else:
            verifier = OidcVerifier.keycloak(settings.oidc_issuer, settings.oidc_audience, settings.jwks_url)
        c = Components(sessionmaker=sm, token_verifier=verifier, dev_issuer=dev_issuer)

        if settings.enable_ingest:
            from minio import Minio
            from sed_ingest.api.app import create_app as create_ingest_app

            sub = create_ingest_app(ingest_settings, token_verifier=verifier)
            await stack.enter_async_context(sub.router.lifespan_context(sub))
            c.ingest_pipeline = sub.state.pipeline
            c.files = MinioFileReader(Minio(
                ingest_settings.minio_endpoint, access_key=ingest_settings.minio_access_key.get_secret_value(),
                secret_key=ingest_settings.minio_secret_key.get_secret_value(), secure=ingest_settings.minio_secure,
            ), settings.download_chunk_bytes)

        if settings.enable_compliance and rag_mod is not None:
            from sed_rag.domain.models import RagError

            comps = await stack.enter_async_context(rag_mod.build_components(rag_settings))
            c.compliance_service, c.corpus_ingestor = comps["service"], comps["ingestor"]
            c.resolve_user = comps["resolve_user"]
            try:
                await comps["index"].ensure_collection(rag_settings.embedding_dim)
            except RagError as exc:     # Qdrant ще не піднявся: застосунок стартує, /readyz покаже стан
                log.error("Qdrant collection check failed: %s", exc.message)

        if settings.enable_routing and routing_mod is not None:
            sub = routing_mod.create_app(routing_settings, token_verifier=_NoTokens())
            await stack.enter_async_context(sub.router.lifespan_context(sub))
            c.routing_service, c.mock_desk = sub.state.service, sub.state.mock_desk
            c.kep_keys = getattr(sub.state, "keys", None)
        yield c


class _NoTokens:
    """Модуль маршрутів не перевіряє токени сам: `get_caller` підмінено спільною автентифікацією."""

    def verify(self, token: str) -> Any:  # pragma: no cover
        raise RuntimeError("not used")


def create_app(settings: Settings | None = None, *, components: Components | None = None) -> FastAPI:
    settings = settings or Settings()
    ingest_settings = IngestSettings()
    rag_mod, routing_mod = _optional_modules()
    rag_settings = _rag_settings(rag_mod)
    routing_settings = _routing_settings(routing_mod) if settings.enable_routing else None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            c = components or await stack.enter_async_context(
                _build(settings, ingest_settings, rag_settings, routing_settings, rag_mod, routing_mod))
            _wire_state(app, settings, c, rag_settings)
            yield

    app = FastAPI(
        title="СЕД ПУЕТ — система електронного документообігу з ШІ",
        version=__version__,
        description=(
            "Єдиний API СЕД Полтавського університету економіки і торгівлі: реєстрація документів з OCR і "
            "вилученням реквізитів за ДСТУ 4163:2020, нормативний аудит проєктів (RAG), маршрути погодження з КЕП, "
            "ручна валідація результатів ШІ (confidence < 0.85). ШІ є асистентом: юридичної сили документ "
            "набуває лише після накладання КЕП посадовою особою."),
        lifespan=lifespan,
        docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json",
    )
    _exception_handlers(app)
    _middleware(app, settings)

    app.include_router(routes_auth.router)
    app.include_router(routes_documents.router)
    if settings.enable_ingest:
        app.include_router(ingest_routes.router)
    if settings.enable_compliance and rag_mod is not None:
        from sed_rag.api.routes import router as rag_router
        _include_filtered(app, rag_router)
    if settings.enable_routing and routing_mod is not None:
        from sed_routing.api.key_routes import router as kep_keys_router
        from sed_routing.api.routes import dev_router, router as routing_router
        app.include_router(routing_router)
        app.include_router(kep_keys_router)
        if routing_settings is not None and routing_settings.kep_allow_mock:
            app.include_router(dev_router)
    _override_module_dependencies(app, ingest_settings)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request) -> JSONResponse:
        svc: AppServices = request.app.state.sed
        checks: dict[str, str] = {}
        try:
            async with svc.documents.sessionmaker() as s:
                await s.execute(text("SELECT 1"))
            checks["database"] = "ok"
        except Exception as exc:  # noqa: BLE001
            checks["database"] = f"error: {type(exc).__name__}"
        checks["ingest"] = "ok" if request.app.state.pipeline is not None else "disabled"
        checks["compliance"] = "ok" if svc.compliance is not None else "disabled"
        checks["routing"] = "ok" if svc.routing is not None else "disabled"
        ok = checks["database"] == "ok"
        return JSONResponse({"status": "ok" if ok else "degraded", "checks": checks}, status_code=200 if ok else 503)

    if settings.serve_web:
        _web(app, settings)
    return app


def _rag_settings(rag_mod: Any) -> Any:
    if rag_mod is None:
        return None
    from sed_rag.config import Settings as RagSettings
    return RagSettings()


def _routing_settings(routing_mod: Any) -> Any:
    if routing_mod is None:
        return None
    from sed_routing.config import Settings as RoutingSettings
    return RoutingSettings()


def _wire_state(app: FastAPI, settings: Settings, c: Components, rag_settings: Any) -> None:
    corpus_admin = frozenset(rag_settings.corpus_admin_roles) if rag_settings is not None else frozenset()
    app.state.sed = AppServices(
        settings=settings,
        users=SqlUserDirectory(c.sessionmaker, require_token_roles=settings.require_token_roles,
                               jit_provisioning=settings.jit_provisioning, tree_ttl_s=settings.org_tree_ttl_s),
        documents=SqlDocumentReader(c.sessionmaker), review=SqlReviewRepository(c.sessionmaker),
        files=c.files, dev_issuer=c.dev_issuer, compliance=c.compliance_service, routing=c.routing_service,
        corpus_admin_roles=corpus_admin, kep_mock=c.mock_desk is not None,
    )
    # Стан, який читають маршрути модулів (`request.app.state.*`).
    app.state.token_verifier = c.token_verifier
    app.state.pipeline = c.ingest_pipeline
    app.state.service = c.compliance_service          # sed_rag.api.routes._service
    app.state.ingestor = c.corpus_ingestor
    app.state.resolve_user = c.resolve_user
    app.state.settings = rag_settings                 # sed_rag: corpus_admin_roles
    app.state.mock_desk = c.mock_desk                 # sed_routing: імітація КЕП
    app.state.keys = c.kep_keys                       # sed_routing: ключі КЕП користувачів


def _override_module_dependencies(app: FastAPI, ingest_settings: IngestSettings) -> None:
    app.dependency_overrides[ingest_routes.get_settings] = lambda: ingest_settings

    def pipeline(request: Request) -> Any:
        return require(request.app.state.pipeline, "Інжест документів")

    app.dependency_overrides[ingest_routes.get_pipeline] = pipeline
    try:
        from sed_routing.api import auth as routing_auth, key_routes, routes as routing_routes
        from sed_routing.service import Caller
    except ImportError:  # pragma: no cover
        return

    async def caller(principal: Annotated[AppPrincipal, Depends(get_principal)]) -> Caller:
        return Caller(keycloak_sub=principal.sub, roles=principal.roles)

    def routing_service(request: Request) -> Any:
        return require(request.app.state.sed.routing, "Маршрути погодження")

    app.dependency_overrides[routing_auth.get_caller] = caller
    app.dependency_overrides[routing_routes.get_service] = routing_service

    def kep_keys(request: Request) -> Any:
        return require(request.app.state.keys, "Ключі КЕП")

    app.dependency_overrides[key_routes.get_keys] = kep_keys


def _include_filtered(app: FastAPI, router: Any) -> None:
    from fastapi import APIRouter
    from fastapi.routing import APIRoute

    filtered = APIRouter()
    filtered.routes.extend(r for r in router.routes
                           if not (isinstance(r, APIRoute) and r.path in _HIDDEN_MODULE_PATHS))
    app.include_router(filtered)


def _exception_handlers(app: FastAPI) -> None:
    def problem(status_code: int, detail: str, code: str, **extra: Any) -> JSONResponse:
        return JSONResponse({"detail": detail, "code": code, **extra}, status_code=status_code)

    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return problem(exc.http_status, exc.message, exc.code)

    @app.exception_handler(IngestError)
    async def _ingest_error(_: Request, exc: IngestError) -> JSONResponse:
        if exc.http_status >= 500:
            log.error("ingest failed: %s (%s)", exc.code, exc.message)
        return problem(exc.http_status, exc.message, exc.code)

    try:
        from sed_rag.domain.models import RagError

        @app.exception_handler(RagError)
        async def _rag_error(_: Request, exc: RagError) -> JSONResponse:
            if exc.http_status >= 500:
                log.error("compliance failed: %s (%s)", exc.code, exc.message)
            return problem(exc.http_status, exc.message, exc.code)
    except ImportError:  # pragma: no cover
        pass
    try:
        from sed_routing.domain.errors import RoutingError

        @app.exception_handler(RoutingError)
        async def _routing_error(_: Request, exc: RoutingError) -> JSONResponse:
            if exc.http_status >= 500:
                log.error("routing failed: %s (%s)", exc.code, exc.message)
            return problem(exc.http_status, exc.message, exc.code, **exc.details)
    except ImportError:  # pragma: no cover
        pass

    @app.exception_handler(DBAPIError)
    async def _db_error(_: Request, exc: DBAPIError) -> JSONResponse:
        """Інваріанти БД (тригери, CHECK) — останній рубіж; користувачу — зрозуміле повідомлення."""
        sqlstate = getattr(exc.orig, "sqlstate", None) or ""
        log.warning("database rejected request: %s %s", sqlstate, str(exc.orig).splitlines()[0] if exc.orig else "")
        if sqlstate == "42501":
            return problem(403, "Дію заборонено правилами СЕД (людина в контурі прийняття рішень)", "db_forbidden")
        if sqlstate.startswith("23") or sqlstate == "40001":
            return problem(409, "Дію відхилено правилами цілісності СЕД: дані змінилися або порушують обмеження",
                           "db_conflict")
        if isinstance(exc.orig, Exception) and exc.connection_invalidated:
            return problem(503, "База даних тимчасово недоступна", "db_unavailable")
        log.exception("database error")
        return problem(500, "Внутрішня помилка бази даних", "db_error")


def _csp(settings: Settings) -> str:
    """Скрипти лише власні; `connect-src` дозволяє обмін кодом на токен з Keycloak (PKCE)."""
    connect = "'self'"
    if settings.auth_mode is AuthMode.KEYCLOAK:
        from urllib.parse import urlsplit
        parts = urlsplit(settings.oidc_issuer)
        connect += f" {parts.scheme}://{parts.netloc}"
    return ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
            f"connect-src {connect}; frame-src 'self' blob:; object-src blob:; base-uri 'none'; "
            "form-action 'self'; frame-ancestors 'none'")


def _middleware(app: FastAPI, settings: Settings) -> None:
    csp = _csp(settings)
    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=False,
                           allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                           allow_headers=["Authorization", "Content-Type", "X-Request-ID"])

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        response = await call_next(request)
        h = response.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("Referrer-Policy", "no-referrer")
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if request.url.path.startswith("/api/docs"):
            return response    # Swagger UI тягне скрипти з CDN
        h.setdefault("Content-Security-Policy", csp)
        h.setdefault("X-Frame-Options", "DENY")
        if request.url.path.startswith("/api/"):
            h.setdefault("Cache-Control", "no-store")
        return response


def _web(app: FastAPI, settings: Settings) -> None:
    root = settings.web_root
    app.mount("/static", StaticFiles(directory=root), name="static")

    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(root / "index.html", headers={"Cache-Control": "no-cache"})
