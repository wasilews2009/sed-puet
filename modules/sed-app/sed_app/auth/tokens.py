"""Перевірка і (лише на dev-стенді) видача JWT.

Продакшн: токени видає Keycloak (realm `puet`), застосунок перевіряє підпис за JWKS, issuer,
audience і строк дії. Realm-ролі токена — 9 функціональних ролей СЕД (`sed_db.enums.UserRole`)
(інших ролей СЕД не використовує; права на нормативний корпус — у ректора і проректора).

Dev: застосунок сам підписує RS256-токени демо-користувачам. Формат claims той самий, що в
Keycloak, тож маршрути модулів (`sed_ingest`, `sed_rag`) працюють без змін.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterable
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from sed_ingest.api.auth import InvalidToken, Principal

_ALGORITHMS = ["RS256", "PS256", "ES256"]


class AppPrincipal(Principal):
    """`Principal` модулів + атрибути профілю для першого входу (JIT-реєстрації)."""

    email: str | None = None
    given_name: str | None = None
    family_name: str | None = None


class OidcVerifier:
    """Реалізує `sed_ingest.api.auth.TokenVerifier`."""

    def __init__(self, issuer: str, audience: str, key_resolver: Callable[[str], Any]) -> None:
        self.issuer = issuer
        self.audience = audience
        self._key_for = key_resolver

    @classmethod
    def keycloak(cls, issuer: str, audience: str, jwks_url: str) -> OidcVerifier:
        client = jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=300)
        return cls(issuer, audience, lambda token: client.get_signing_key_from_jwt(token).key)

    def verify(self, token: str) -> AppPrincipal:
        try:
            claims = jwt.decode(
                token, self._key_for(token), algorithms=_ALGORITHMS, audience=self.audience,
                issuer=self.issuer, options={"require": ["exp", "iat", "sub", "iss"]}, leeway=30,
            )
            sub = uuid.UUID(claims["sub"])
        except (jwt.PyJWTError, ValueError, KeyError) as exc:
            raise InvalidToken(str(exc)) from exc
        roles = frozenset((claims.get("realm_access") or {}).get("roles") or [])
        return AppPrincipal(
            sub=sub, username=claims.get("preferred_username"), roles=roles,
            email=claims.get("email"), given_name=claims.get("given_name"),
            family_name=claims.get("family_name"),
        )


class DevTokenIssuer:
    """Підписувач токенів демо-стенду. Ключ живе в пам'яті процесу (або з налаштувань)."""

    def __init__(self, issuer: str, audience: str, ttl_s: int, private_key_pem: str | None = None) -> None:
        self.issuer = issuer
        self.audience = audience
        self.ttl_s = ttl_s
        if private_key_pem:
            key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
            if not isinstance(key, rsa.RSAPrivateKey):
                raise ValueError("Ключ dev-токенів має бути RSA")
            self._key = key
        else:
            self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.public_key = self._key.public_key()

    def issue(self, *, sub: uuid.UUID, username: str, email: str, given_name: str,
              family_name: str, roles: Iterable[str]) -> tuple[str, int]:
        now = int(time.time())
        claims = {
            "iss": self.issuer, "aud": self.audience, "sub": str(sub), "iat": now, "nbf": now,
            "exp": now + self.ttl_s, "jti": uuid.uuid4().hex, "typ": "Bearer",
            "preferred_username": username, "email": email, "given_name": given_name,
            "family_name": family_name, "realm_access": {"roles": sorted(set(roles))},
        }
        return jwt.encode(claims, self._key, algorithm="RS256"), self.ttl_s

    def verifier(self) -> OidcVerifier:
        return OidcVerifier(self.issuer, self.audience, lambda _token: self.public_key)
