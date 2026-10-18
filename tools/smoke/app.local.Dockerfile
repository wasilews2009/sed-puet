# Образ застосунку СЕД ПУЕТ (API sed-app + модулі db-schema, ingest-pipeline, rag-compliance, approval-routing).
# Контекст збірки — корінь релізу:  docker build -f docker/app.Dockerfile -t puet/sed-app:0.1.0 .
# Цей самий образ запускає і міграції (сервіс migrate у docker-compose.yml).
# ЛИШЕ ДЛЯ ПЕРЕВІРКИ В ІЗОЛЬОВАНОМУ СЕРЕДОВИЩІ (репозиторії Debian недоступні): той самий Python 3.13 з офіційного образу,
# Tesseract з Ubuntu 24.04. Решта ідентична docker/app.Dockerfile.
FROM python:3.13-slim-bookworm AS py
FROM ubuntu:24.04
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 LANG=C.UTF-8
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-ukr tesseract-ocr-eng tzdata libssl3t64 libffi8 libsqlite3-0 libbz2-1.0 liblzma5 zlib1g libexpat1 libncursesw6 libreadline8t64 ca-certificates  && rm -rf /var/lib/apt/lists/*
COPY --from=py /usr/local /usr/local
RUN ldconfig && python3.13 -c "import ssl, sqlite3, ctypes; print('py ok')"

WORKDIR /opt/sed
# Спершу лише залежності (кеш шару). constraints.txt — точні версії, з якими реліз перевірено.
COPY constraints.txt ./
COPY modules/db-schema/requirements.txt db-schema/
COPY modules/ingest-pipeline/requirements.txt ingest-pipeline/
COPY modules/rag-compliance/requirements.txt rag-compliance/
COPY modules/approval-routing/requirements.txt approval-routing/
COPY modules/sed-app/requirements.txt sed-app/
RUN --mount=type=secret,id=ca,target=/run/ca.pem cd sed-app && PIP_CERT=/run/ca.pem pip install -c /opt/sed/constraints.txt -r requirements.txt

COPY modules/db-schema/alembic.ini db-schema/
COPY modules/db-schema/migrations/ db-schema/migrations/
COPY modules/db-schema/sed_db/ db-schema/sed_db/
COPY modules/ingest-pipeline/sed_ingest/ ingest-pipeline/sed_ingest/
COPY modules/rag-compliance/sed_rag/ rag-compliance/sed_rag/
COPY modules/rag-compliance/corpus/ rag-compliance/corpus/
COPY modules/approval-routing/sed_routing/ approval-routing/sed_routing/
COPY modules/approval-routing/templates/ approval-routing/templates/
COPY modules/approval-routing/schema_proposal/ approval-routing/schema_proposal/
COPY modules/sed-app/sed_app/ sed-app/sed_app/

ENV PYTHONPATH=/opt/sed/sed-app:/opt/sed/db-schema:/opt/sed/ingest-pipeline:/opt/sed/rag-compliance:/opt/sed/approval-routing \
    SED_ROUTING_TEMPLATES_DIR=/opt/sed/approval-routing/templates

RUN useradd --system --uid 10001 --home /nonexistent sed
USER sed
WORKDIR /opt/sed/sed-app
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=4).status==200 else 1)"
CMD ["uvicorn", "sed_app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--no-server-header"]
