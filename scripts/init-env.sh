#!/usr/bin/env sh
# Створює .env з .env.example, замінюючи кожне CHANGE_ME випадковим секретом.
#   scripts/init-env.sh            (не перезаписує наявний .env)
set -eu
cd "$(dirname "$0")/.."
[ -f .env ] && { echo ".env уже існує — видаліть його, якщо хочете згенерувати заново"; exit 1; }
command -v openssl >/dev/null || { echo "потрібен openssl"; exit 1; }
umask 077
: > .env
while IFS= read -r line; do
  case "$line" in
    MINIO_KMS_SECRET_KEY=*CHANGE_ME*) line="MINIO_KMS_SECRET_KEY=sed-key:$(openssl rand -base64 32)" ;;
    *CHANGE_ME*) line="${line%%=*}=$(openssl rand -hex 32)" ;;
  esac
  printf '%s\n' "$line" >> .env
done < .env.example
echo "Створено .env (права 600). Перевірте адреси SED_PUBLIC_URL і KEYCLOAK_PUBLIC_URL."
