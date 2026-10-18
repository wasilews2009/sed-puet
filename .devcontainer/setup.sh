#!/usr/bin/env bash
# Codespaces, postCreateCommand (один раз): .env під адресу codespace + збірка образів.
# Локально можна запустити так само: без змінних Codespaces адреса буде http://localhost:8080.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -n "${CODESPACE_NAME:-}" ]; then
  PUBLIC_URL="https://${CODESPACE_NAME}-8080.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-app.github.dev}"
  SCHEME=https
else
  PUBLIC_URL="${SED_PUBLIC_URL:-http://localhost:8080}"
  SCHEME="${PUBLIC_URL%%:*}"
fi
# Модель для CPU: Mistral Small 24B потребує ≥ 32 ГБ RAM, тому в codespace (16 ГБ) — компактна багатомовна модель.
MODEL="${SED_CODESPACES_LLM_MODEL:-gemma3:4b}"

set_env() {  # set_env КЛЮЧ ЗНАЧЕННЯ — замінити рядок у .env або дописати
  local k="$1" v="$2"
  if grep -q "^${k}=" .env; then
    local tmp; tmp="$(mktemp)"
    awk -v k="$k" -v v="$v" 'BEGIN{FS=OFS="="} $1==k{print k "=" v; next} {print}' .env > "$tmp"
    cat "$tmp" > .env && rm -f "$tmp"
  else
    printf '%s=%s\n' "$k" "$v" >> .env
  fi
}

[ -f .env ] || sh scripts/init-env.sh
set_env COMPOSE_FILE docker-compose.yml:.devcontainer/compose.codespaces.yml
set_env COMPOSE_PROFILES cpu
set_env SED_PUBLIC_URL "$PUBLIC_URL"
set_env SED_PUBLIC_SCHEME "$SCHEME"
set_env KEYCLOAK_PUBLIC_URL "$PUBLIC_URL/auth"
set_env SED_APP_OIDC_JWKS_URL http://keycloak:8080/auth/realms/puet/protocol/openid-connect/certs
set_env SED_LLM_MODEL "$MODEL"
set_env SED_INGEST_LLM_BASE_URL http://llm:11434/v1
set_env SED_RAG_LLM_BASE_URL http://llm:11434/v1
set_env SED_INGEST_LLM_PROVIDER ollama
set_env SED_RAG_LLM_PROVIDER ollama
echo "Адреса стенду: $PUBLIC_URL (модель LLM: $MODEL)"

# Docker-in-Docker може стартувати на кілька секунд пізніше за postCreateCommand
for _ in $(seq 1 60); do docker info >/dev/null 2>&1 && break; sleep 2; done
docker compose build
docker compose pull --ignore-buildable --quiet
