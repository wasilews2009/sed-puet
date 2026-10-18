"""Налаштування модуля (змінні середовища з префіксом `SED_RAG_`)."""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class RetrievalSettings(BaseSettings):
    """Параметри гібридного пошуку. Зберігаються в `compliance_checks.retrieval_config`,
    тож кожну перевірку можна відтворити з тими самими параметрами."""

    model_config = SettingsConfigDict(env_prefix="SED_RAG_RETRIEVAL_")

    dense_k: int = Field(default=30, ge=1, le=200)       # кандидатів з dense-пошуку
    sparse_k: int = Field(default=30, ge=1, le=200)      # кандидатів з BM25
    rrf_k: int = Field(default=60, ge=1)                 # константа Reciprocal Rank Fusion
    rerank_top_n: int = Field(default=20, ge=1)          # скільки злитих кандидатів віддати reranker-у
    final_k: int = Field(default=6, ge=1, le=12)         # норм на один пункт проєкту в промпті
    max_per_source: int = Field(default=4, ge=1)         # різноманіття: не більше N фрагментів одного акта
    # Сила пошуку для одного пункту. Нижче — пункт переформульовується (CRAG) і шукається ще раз.
    min_dense_score: float = Field(default=0.45, ge=0, le=1)
    min_rerank_score: float = Field(default=0.10, ge=0, le=1)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SED_RAG_", env_nested_delimiter="__")

    # --- Qdrant
    qdrant_url: str = "http://qdrant:6333"
    qdrant_api_key: SecretStr | None = None
    qdrant_collection: str = "sed_regulations"
    qdrant_timeout_s: int = 30

    # --- Ембединги (OpenAI-сумісний /v1/embeddings: vLLM `--task embed`, Ollama, TEI)
    embedding_base_url: str = "http://embeddings:8000/v1"
    embedding_api_key: SecretStr = SecretStr("not-needed")
    embedding_model: str = "BAAI/bge-m3"
    embedding_model_version: str = "2024-01"
    embedding_provider: str = "vllm"                     # sed_db.enums.ModelProvider
    embedding_dim: int = 1024                            # = sed_db.models.ai.EMBEDDING_DIM
    embedding_query_prefix: str = ""                     # multilingual-e5: "query: "
    embedding_passage_prefix: str = ""                   # multilingual-e5: "passage: "
    embedding_batch_size: int = Field(default=32, ge=1, le=256)

    # --- Reranker (необов'язковий; TEI `/rerank`, напр. BAAI/bge-reranker-v2-m3)
    reranker_url: str | None = None

    # --- LLM (OpenAI-сумісний endpoint vLLM / Ollama, той самий, що в ingest-pipeline)
    llm_base_url: str = "http://llm:8000/v1"
    llm_api_key: SecretStr = SecretStr("not-needed")
    llm_model: str = "mistral-small-3.1-24b-instruct-uk"
    llm_model_version: str = "2503"
    llm_provider: str = "vllm"
    llm_timeout_s: float = 120.0
    llm_max_output_tokens: int = 3072
    llm_seed: int = 42
    llm_concurrency: int = Field(default=4, ge=1, le=32)  # паралельних викликів на одну перевірку

    # --- Перевірка
    max_draft_chars: int = Field(default=60_000, ge=1000)
    max_provisions: int = Field(default=40, ge=1)        # пунктів проєкту на одну перевірку
    provision_max_chars: int = Field(default=1800, ge=300)
    norm_max_chars: int = Field(default=1400, ge=300)    # обрізання фрагмента норми в промпті
    rewrite_weak_queries: bool = True                    # CRAG: переформулювати пункти зі слабким пошуком

    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)

    # --- Keycloak
    oidc_issuer: str = "https://sso.puet.local/realms/puet"
    oidc_audience: str = "sed-api"
    oidc_jwks_url: str = "https://sso.puet.local/realms/puet/protocol/openid-connect/certs"
    # Хто може змінювати нормативний корпус (realm-ролі Keycloak).
    corpus_admin_roles: frozenset[str] = frozenset({"sed-regulations-admin"})

    # --- БД
    database_url: str = "postgresql+psycopg://sed_app@db/sed"
