"""Автентифікація: Bearer JWT від Keycloak (OIDC). Перевіряються підпис (JWKS), issuer,
audience, строк дії. Права на підрозділ перевіряє репозиторій за `user_roles` у БД."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Annotated, Any, Protocol

import anyio
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

_ALGORITHMS = ["RS256", "PS256", "ES256"]


class Principal(BaseModel):
    sub: uuid.UUID
    username: str | None = None
    roles: frozenset[str] = frozenset()


class TokenVerifier(Protocol):
    def verify(self, token: str) -> Principal: ...


class InvalidToken(Exception):
    pass


class KeycloakJwtVerifier:
    def __init__(
        self,
        issuer: str,
        audience: str,
        jwks_url: str | None = None,
        key_resolver: Callable[[str], Any] | None = None,
    ) -> None:
        self.issuer = issuer
        self.audience = audience
        if key_resolver is None:
            client = jwt.PyJWKClient(jwks_url or f"{issuer}/protocol/openid-connect/certs",
                                     cache_keys=True, lifespan=300)
            key_resolver = lambda token: client.get_signing_key_from_jwt(token).key  # noqa: E731
        self._key_for = key_resolver

    def verify(self, token: str) -> Principal:
        try:
            claims = jwt.decode(
                token, self._key_for(token), algorithms=_ALGORITHMS,
                audience=self.audience, issuer=self.issuer,
                options={"require": ["exp", "iat", "sub", "iss"]},
            )
            sub = uuid.UUID(claims["sub"])
        except (jwt.PyJWTError, ValueError, KeyError) as exc:
            raise InvalidToken(str(exc)) from exc
        roles = frozenset((claims.get("realm_access") or {}).get("roles") or [])
        return Principal(sub=sub, username=claims.get("preferred_username"), roles=roles)


_bearer = HTTPBearer(auto_error=False, bearerFormat="JWT", description="Access token Keycloak (realm puet)")


async def get_principal(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Потрібна автентифікація",
                            headers={"WWW-Authenticate": "Bearer"})
    verifier: TokenVerifier = request.app.state.token_verifier
    try:
        # PyJWKClient може звертатися до Keycloak по JWKS — не блокуємо event loop.
        return await anyio.to_thread.run_sync(verifier.verify, creds.credentials)
    except InvalidToken:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Недійсний або прострочений токен",
                            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'}) from None
