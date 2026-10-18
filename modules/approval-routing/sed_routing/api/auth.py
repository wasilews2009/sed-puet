"""Автентифікація: Bearer JWT від Keycloak (OIDC) — та сама схема, що в sed_ingest.api.auth.

Перевіряються підпис (JWKS), issuer, audience, строк дії. Права на документ і крок перевіряє
сервіс за даними БД (автор, ініціатор, учасник кроку).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Annotated, Any, Protocol

import anyio
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..service import Caller

_ALGORITHMS = ["RS256", "PS256", "ES256"]


class TokenVerifier(Protocol):
    def verify(self, token: str) -> Caller: ...


class InvalidToken(Exception):
    pass


class KeycloakJwtVerifier:
    def __init__(self, issuer: str, audience: str, jwks_url: str | None = None,
                 key_resolver: Callable[[str], Any] | None = None) -> None:
        self.issuer, self.audience = issuer, audience
        if key_resolver is None:
            client = jwt.PyJWKClient(jwks_url or f"{issuer}/protocol/openid-connect/certs",
                                     cache_keys=True, lifespan=300)
            key_resolver = lambda token: client.get_signing_key_from_jwt(token).key  # noqa: E731
        self._key_for = key_resolver

    def verify(self, token: str) -> Caller:
        try:
            claims = jwt.decode(token, self._key_for(token), algorithms=_ALGORITHMS, audience=self.audience,
                                issuer=self.issuer, options={"require": ["exp", "iat", "sub", "iss"]})
            sub = uuid.UUID(claims["sub"])
        except (jwt.PyJWTError, ValueError, KeyError) as exc:
            raise InvalidToken(str(exc)) from exc
        roles = frozenset((claims.get("realm_access") or {}).get("roles") or [])
        return Caller(keycloak_sub=sub, roles=roles)


_bearer = HTTPBearer(auto_error=False, bearerFormat="JWT", description="Access token Keycloak (realm puet)")


async def get_caller(
    request: Request, creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Caller:
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Потрібна автентифікація",
                            headers={"WWW-Authenticate": "Bearer"})
    verifier: TokenVerifier = request.app.state.token_verifier
    try:
        return await anyio.to_thread.run_sync(verifier.verify, creds.credentials)
    except InvalidToken:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Недійсний або прострочений токен",
                            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'}) from None
