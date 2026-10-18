# Смоук-тест релізу в ізольованому середовищі (для перевірки в ізольованому середовищі)

Для середовищ, де закриті Hugging Face, quay.io, ghcr.io і репозиторії Debian (як у хмарному контейнері Claude):
`app.local.Dockerfile` — образ API з Tesseract з Ubuntu; `llm-stub/` — заглушка OpenAI-сумісних LLM і ембедингів
(не модель!); `compose.local.yml` — дзеркала MinIO/Keycloak з Docker Hub; `release.spec.ts` — сценарій Playwright
через вхід Keycloak.

    sudo dockerd &                                             # якщо демона немає
    docker build --network host --secret id=ca,src=/root/.ccr/ca-bundle.crt -f tools/smoke/app.local.Dockerfile -t puet/sed-app:0.1.0 .
    docker build -f docker/web.Dockerfile -t puet/sed-web:0.1.0 .
    (cd tools/smoke/llm-stub && docker build -t local/llm-stub .)
    scripts/init-env.sh && docker compose -f docker-compose.yml -f tools/smoke/compose.local.yml up -d --no-build
    docker compose exec app python -m sed_rag.ingestion.cli ingest-manifest /opt/sed/rag-compliance/corpus/manifest.toml
    # у копії sed-frontend з node_modules: покладіть release.spec.ts у e2e-release/, конфіг поруч
    SED_FIXTURES=…/sed-frontend/dev/fixtures SED_SHOTS=/tmp/shots PW_CHROMIUM=/opt/pw-browsers/chromium-*/chrome-linux/chrome \
      npx playwright test -c playwright.release.config.ts
