#!/usr/bin/env bash
# Codespaces, postStartCommand (кожен старт): стек, модель LLM, нормативний корпус.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || bash .devcontainer/setup.sh

for _ in $(seq 1 60); do docker info >/dev/null 2>&1 && break; sleep 2; done
docker compose up -d

MODEL="$(sed -n 's/^SED_LLM_MODEL=//p' .env)"
echo "Завантаження моделі $MODEL (лише перший раз, кілька хвилин)…"
docker compose exec -T llm-ollama ollama pull "$MODEL" >/dev/null

echo "Очікування веб-інтерфейсу…"
for _ in $(seq 1 120); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q web)" 2>/dev/null)" = healthy ] && break
  sleep 5
done

# Корпус індексується один раз у фоні: сервер ембедингів спершу завантажує bge-m3 (≈ 2 ГБ).
if [ ! -f .devcontainer/.corpus-indexed ]; then
  nohup sh -c 'for i in $(seq 1 60); do
      docker compose exec -T app python -m sed_rag.ingestion.cli ingest-manifest /opt/sed/rag-compliance/corpus/manifest.toml \
        && touch .devcontainer/.corpus-indexed && exit 0
      sleep 20
    done' > .devcontainer/corpus-index.log 2>&1 &
fi

URL="$(sed -n 's/^SED_PUBLIC_URL=//p' .env)"
cat <<MSG

  СЕД ПУЕТ запущено: $URL  (вкладка Ports → 8080)
  Якщо браузер показує HTTP ERROR 401: Ports → правою кнопкою на 8080 → Port Visibility → Public
  (стенд стане доступним кожному, хто знає адресу; після показу поверніть Private або зупиніть codespace).
  Вхід: «Увійти через SSO ПУЕТ», демо-користувачі rector, vicerector, director, lecturer, clerk …, пароль demo.
  Індексація нормативного корпусу: .devcontainer/corpus-index.log

MSG
