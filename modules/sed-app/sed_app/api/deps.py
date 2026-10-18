"""Залежності FastAPI: сервіси застосунку, актор запиту, область доступу, X-Request-ID."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import Depends, Request

from sed_ingest.api.auth import get_principal

from ..adapters.file_storage import FileReader
from ..adapters.sql_documents import SqlDocumentReader
from ..adapters.sql_review import SqlReviewRepository
from ..adapters.sql_users import SqlUserDirectory
from ..auth.tokens import AppPrincipal, DevTokenIssuer
from ..config import Settings
from ..domain.access import AccessScope, Actor, DocumentFacts, can_review, can_view, scope_for
from ..domain.errors import Forbidden, ModuleUnavailable, NotFound

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


@dataclass
class AppServices:
    settings: Settings
    users: SqlUserDirectory
    documents: SqlDocumentReader
    review: SqlReviewRepository
    files: FileReader | None = None
    dev_issuer: DevTokenIssuer | None = None
    compliance: Any = None          # sed_rag.service.ComplianceService
    routing: Any = None             # адаптер модуля маршрутів (див. api/routes_approvals.py)
    corpus_admin_roles: frozenset[str] = field(default_factory=frozenset)
    kep_mock: bool = False


def services(request: Request) -> AppServices:
    return request.app.state.sed  # type: ignore[no-any-return]


Services = Annotated[AppServices, Depends(services)]


async def current_actor(request: Request, principal: Annotated[AppPrincipal, Depends(get_principal)],
                        svc: Services) -> Actor:
    actor = await svc.users.actor(principal)
    request.state.actor_user_id = actor.user_id    # для журналу запитів
    return actor


CurrentActor = Annotated[Actor, Depends(current_actor)]


async def access_scope(actor: CurrentActor, svc: Services) -> AccessScope:
    return scope_for(actor, await svc.users.org_tree())


Scope = Annotated[AccessScope, Depends(access_scope)]


def request_id(request: Request) -> str:
    header = request.headers.get("X-Request-ID", "")
    return header if _REQUEST_ID.match(header) else uuid.uuid4().hex


RequestId = Annotated[str, Depends(request_id)]


async def viewable(svc: AppServices, scope: AccessScope, document_id: uuid.UUID) -> DocumentFacts:
    facts = await svc.documents.facts(document_id)
    if not can_view(scope, facts):
        # 404 замість 403: не підтверджуємо існування документа, до якого немає доступу.
        raise NotFound("Документ не знайдено")
    return facts


async def reviewable(svc: AppServices, scope: AccessScope, document_id: uuid.UUID) -> DocumentFacts:
    facts = await viewable(svc, scope, document_id)
    if not can_review(scope, facts):
        raise Forbidden("Валідувати результати ШІ цього документа може автор, співробітник або керівник підрозділу; "
                        "після підписання зміни заборонено")
    return facts


def require(module: Any, name: str) -> Any:
    if module is None:
        raise ModuleUnavailable(f"Модуль «{name}» не підключено на цьому стенді")
    return module
