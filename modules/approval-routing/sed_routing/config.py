"""Налаштування модуля (змінні середовища з префіксом `SED_ROUTING_`)."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SED_ROUTING_", env_nested_delimiter="__")

    environment: str = Field(default="production", pattern="^(production|staging|development|test)$")

    # --- Шаблони маршрутів
    templates_dir: Path = ROOT / "templates"
    sync_templates_on_startup: bool = True

    # --- КЕП
    kep_trust_anchors_pem: Path = Path("/etc/sed/kep/trusted-ca.pem")  # сертифікати КНЕДП з Довірчого списку
    kep_tax_id_hmac_secret: SecretStr = SecretStr("")                  # ≥ 32 байт, з Vault
    # Застаріле (до таблиці user_kep_identities): {"<user_id>": "<HMAC РНОКПП>"}. Ігнорується, якщо
    # увімкнено kep_registry_sql.
    kep_bindings_file: Path | None = None
    kep_registry_sql: bool = True            # реєстр ключів у БД (schema_proposal/0003_kep_keys.sql)
    kep_challenge_secret: SecretStr = SecretStr("")  # ≥ 32 байт, з Vault; для доказу володіння ключем
    kep_require_name_match: bool = True      # ПІБ у сертифікаті = ПІБ у картці користувача
    kep_admin_role: str = "sed-kep-admin"    # роль Keycloak: відкликати чужі ключі
    # Сертифікований сервіс перевірки ДСТУ 4145 (ІІТ / Дія.Підпис), див. kep/external.py.
    kep_dstu_verifier_url: str | None = None

    # --- Внутрішній ЦСК «АІ-СЕД ПУЕТ» (не КЕП; для кроків, де шаблон дозволяє рівень internal)
    internal_ca_enabled: bool = False
    internal_ca_key_pem: Path = Path("/etc/sed/internal-ca/issuing.key.pem")   # зашифрований
    internal_ca_key_password: SecretStr = SecretStr("")
    internal_ca_chain_pem: Path = Path("/etc/sed/internal-ca/chain.pem")      # видавальний + кореневий
    internal_ca_validity_days: int = Field(default=365, ge=1, le=3 * 365)
    internal_ca_public_url: str = "https://sed.puet.edu.ua/api/v1/kep/internal"
    kep_allow_mock: bool = False             # імітаційний КНЕДП: лише development/test
    kep_require_qualified: bool = True
    kep_max_container_bytes: int = Field(default=256 * 1024, ge=4096, le=10 * 1024 * 1024)
    kep_clock_skew_s: int = Field(default=300, ge=0, le=3600)

    # --- MinIO (контейнери підписів)
    minio_endpoint: str = "minio:9000"
    minio_access_key: SecretStr = SecretStr("")
    minio_secret_key: SecretStr = SecretStr("")
    minio_secure: bool = True
    minio_signatures_bucket: str = "sed-signatures"
    minio_sse: bool = True

    # --- Правила
    block_on_critical_findings: bool = True  # відкриті критичні зауваження RAG-аудиту блокують запуск
    ai_service_roles: frozenset[str] = frozenset({"sed-ai-service"})

    # --- Keycloak
    oidc_issuer: str = "https://sso.puet.local/realms/puet"
    oidc_audience: str = "sed-api"
    oidc_jwks_url: str = "https://sso.puet.local/realms/puet/protocol/openid-connect/certs"

    # --- БД
    database_url: str = "postgresql+psycopg://sed_app@db/sed"

    @model_validator(mode="after")
    def _no_mock_in_production(self) -> Settings:
        if self.kep_allow_mock and self.environment in {"production", "staging"}:
            raise ValueError("Імітаційний КНЕДП заборонено в production/staging")
        if self.environment in {"production", "staging"} and len(self.kep_challenge_secret.get_secret_value()) < 32:
            raise ValueError("SED_ROUTING_KEP_CHALLENGE_SECRET має бути щонайменше 32 байти")
        return self
