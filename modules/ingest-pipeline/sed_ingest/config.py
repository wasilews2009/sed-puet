"""Налаштування модуля (змінні середовища з префіксом `SED_INGEST_`)."""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from .domain.requisites import DocumentKind


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SED_INGEST_", env_nested_delimiter="__")

    # --- Файл
    max_file_bytes: int = Field(default=50 * 1024 * 1024, le=200 * 1024 * 1024)  # БД дозволяє до 200 МБ
    max_pages: int = Field(default=30, ge=1, description="Сторінок для синхронної обробки")
    max_image_pixels: int = 60_000_000  # захист від decompression bomb (≈ A3 @ 600 dpi)

    # --- MinIO
    minio_endpoint: str = "minio:9000"
    minio_access_key: SecretStr = SecretStr("")
    minio_secret_key: SecretStr = SecretStr("")
    minio_secure: bool = True
    minio_bucket: str = "sed-documents"
    minio_sse: bool = True              # SSE-S3 (потрібен налаштований KMS у MinIO)

    # --- OCR
    tesseract_lang: str = "ukr+eng"
    ocr_dpi: int = Field(default=300, ge=150, le=600)
    ocr_page_timeout_s: int = 60
    text_layer_min_chars: int = 40      # менше «живих» літер на сторінці → сторінку OCR-имо

    # --- LLM (OpenAI-сумісний endpoint vLLM / Ollama)
    llm_base_url: str = "http://llm:8000/v1"
    llm_api_key: SecretStr = SecretStr("not-needed")
    llm_model: str = "mistral-small-3.1-24b-instruct-uk"
    llm_model_version: str = "2503"
    llm_provider: str = "vllm"          # sed_db.enums.ModelProvider
    llm_timeout_s: float = 120.0
    llm_max_output_tokens: int = 2048
    llm_max_input_chars: int = 24_000   # початок + кінець документа, якщо довший
    llm_seed: int = 42

    # --- Keycloak
    oidc_issuer: str = "https://sso.puet.local/realms/puet"
    oidc_audience: str = "sed-api"
    oidc_jwks_url: str = "https://sso.puet.local/realms/puet/protocol/openid-connect/certs"

    # --- БД
    database_url: str = "postgresql+psycopg://sed_app@db/sed"

    # Вид документа → `document_types.code`. Довідник видів наповнює seed-міграція схеми БД.
    document_type_codes: dict[DocumentKind, str] = Field(default_factory=lambda: {
        DocumentKind.ORDER_MAIN: "ORDER_MAIN",
        DocumentKind.ORDER_PERSONNEL: "ORDER_PERSONNEL",
        DocumentKind.ORDER_STUDENTS: "ORDER_STUDENTS",
        DocumentKind.DIRECTIVE: "DIRECTIVE",
        DocumentKind.APPLICATION: "APPLICATION",
        DocumentKind.MEMO: "MEMO",
        DocumentKind.REPORT_MEMO: "REPORT_MEMO",
        DocumentKind.EXPLANATORY_NOTE: "EXPLANATORY_NOTE",
        DocumentKind.LETTER: "LETTER",
        DocumentKind.PROTOCOL: "PROTOCOL",
        DocumentKind.REGULATION: "REGULATION",
        DocumentKind.OTHER: "UNSORTED",
    })
    # Куди класти документ, якщо вид не визначено або впевненість класифікації < порогу.
    fallback_document_type_code: str = "UNSORTED"
