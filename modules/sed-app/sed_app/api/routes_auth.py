"""Автентифікація і профіль поточного користувача."""

from __future__ import annotations

from fastapi import APIRouter

from ..adapters.sql_users import display_name
from ..config import AuthMode
from ..domain.errors import Forbidden, NotFound
from ..domain.labels import label
from .deps import CurrentActor, Services
from .schemas import AuthConfigOut, DevLoginIn, DevUserOut, MeOut, Permissions, RoleOut, TokenOut

router = APIRouter(prefix="/api/v1", tags=["Автентифікація"])


@router.get("/auth/config", response_model=AuthConfigOut, summary="Як входити: Keycloak (OIDC + PKCE) чи dev-стенд")
async def auth_config(svc: Services) -> AuthConfigOut:
    st = svc.settings
    if st.auth_mode is AuthMode.KEYCLOAK:
        return AuthConfigOut(mode="keycloak", issuer=st.oidc_issuer, client_id=st.oidc_public_client_id,
                             environment=st.environment.value)
    return AuthConfigOut(mode="dev", environment=st.environment.value)


def _dev_only(svc: Services) -> None:
    if svc.settings.auth_mode is not AuthMode.DEV or svc.dev_issuer is None:
        raise NotFound("Доступно лише на dev-стенді")


@router.get("/auth/dev-users", response_model=list[DevUserOut], summary="Демо-користувачі (лише dev-стенд)")
async def dev_users(svc: Services) -> list[DevUserOut]:
    _dev_only(svc)
    return [DevUserOut(id=u.id, display_name=display_name(u.first_name, u.last_name), email=u.email,
                       roles=[label(r) for r in roles])
            for u, roles in await svc.users.dev_users() if roles]


@router.post("/auth/dev-login", response_model=TokenOut, summary="Увійти демо-користувачем (лише dev-стенд)")
async def dev_login(body: DevLoginIn, svc: Services) -> TokenOut:
    _dev_only(svc)
    assert svc.dev_issuer is not None
    found = await svc.users.dev_user(body.user_id)
    if found is None:
        raise Forbidden("Користувача не знайдено або його деактивовано")
    user, roles = found
    realm_roles = [r.value for r in roles]   # лише 9 ролей СЕД, як у realm Keycloak
    token, ttl = svc.dev_issuer.issue(sub=user.keycloak_sub, username=user.email.split("@")[0], email=user.email,
                                      given_name=user.first_name, family_name=user.last_name, roles=realm_roles)
    return TokenOut(access_token=token, expires_in=ttl)


@router.get("/me", response_model=MeOut, summary="Поточний користувач, його ролі та права")
async def me(actor: CurrentActor, svc: Services) -> MeOut:
    units = {u.id: u.name_uk for u in await svc.users.org_units()}
    return MeOut(
        id=actor.user_id, display_name=actor.display_name, email=actor.email,
        primary_org_unit_id=actor.primary_org_unit_id,
        roles=[RoleOut(role=g.role, role_label=label(g.role), org_unit_id=g.org_unit_id,
                       org_unit_name=units.get(g.org_unit_id) if g.org_unit_id else None,
                       scope_note=g.scope_note, is_acting=g.is_acting) for g in actor.grants],
        permissions=Permissions(
            upload_all_units=actor.is_university_wide,
            upload_unit_ids=sorted(u for u in actor.upload_units if u in units),
            corpus_admin=bool(actor.realm_roles & svc.corpus_admin_roles),
            view_all_documents=actor.is_university_wide,
        ),
    )
