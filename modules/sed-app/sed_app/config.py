"""Налаштування застосунку (змінні середовища з префіксом `SED_APP_`).

Модулі, які застосунок збирає, мають власні налаштування з власними префіксами
(`SED_INGEST_`, `SED_RAG_`, `SED_ROUTING_`): docker-compose передає їм ті самі значення БД і LLM.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AuthMode(StrEnum):
    KEYCLOAK = "keycloak"   # продакшн: OIDC Authorization Code + PKCE, токени Keycloak
    DEV = "dev"             # стенд розробника: вхід демо-користувачем, токен підписує сам застосунок


class Environment(StrEnum):
    DEVELOPMENT = "development"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SED_APP_", env_nested_delimiter="__")

    environment: Environment = Environment.PRODUCTION
    database_url: str = "postgresql+psycopg://sed_app@db/sed"
    db_pool_size: int = Field(default=10, ge=1, le=100)

    # --- Автентифікація
    auth_mode: AuthMode = AuthMode.KEYCLOAK
    oidc_issuer: str = "https://sso.puet.local/realms/puet"
    oidc_audience: str = "sed-api"
    oidc_jwks_url: str | None = None
    oidc_public_client_id: str = "sed-web"   # публічний клієнт веб-інтерфейсу (PKCE, без секрету)
    # Ролі з токена мають збігатися з `user_roles` у БД: роль, відкликану в Keycloak, СЕД не враховує
    # навіть до синхронізації. Вимикати лише якщо Keycloak не видає realm-ролі в токені.
    require_token_roles: bool = True
    # Перший вхід користувача, якого ще немає в СЕД: створити картку без ролей (ролі дає адміністратор).
    jit_provisioning: bool = True
    dev_token_ttl_s: int = Field(default=8 * 3600, ge=60)
    dev_token_issuer: str = "sed-app-dev"

    # --- Модулі
    enable_ingest: bool = True
    enable_compliance: bool = True       # потрібні Qdrant і сервер ембедингів
    enable_routing: bool = True          # маршрути погодження і КЕП (../approval-routing)

    # --- Файли (MinIO): віддача оригіналів у браузер іде через застосунок з перевіркою доступу
    download_chunk_bytes: int = 256 * 1024

    # --- Веб-інтерфейс
    serve_web: bool = True
    web_root: Path = Path(__file__).parent / "web" / "static"
    cors_origins: list[str] = Field(default_factory=list)

    # Кеш оргструктури (дерево підрозділів для перевірки доступу керівників)
    org_tree_ttl_s: int = Field(default=60, ge=0)

    # Приватний ключ (PEM) для dev-токенів, якщо вони мають пережити перезапуск;
    # без нього ключ генерується при старті.
    dev_signing_key_pem: SecretStr | None = None

    @model_validator(mode="after")
    def _no_dev_auth_in_production(self) -> Settings:
        if self.environment is Environment.PRODUCTION and self.auth_mode is AuthMode.DEV:
            raise ValueError("SED_APP_AUTH_MODE=dev заборонено в production: вхід без пароля для демо-стенду")
        return self

    @property
    def is_dev(self) -> bool:
        return self.environment is Environment.DEVELOPMENT

    @property
    def jwks_url(self) -> str:
        return self.oidc_jwks_url or f"{self.oidc_issuer}/protocol/openid-connect/certs"
