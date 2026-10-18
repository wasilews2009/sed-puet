# Веб-інтерфейс СЕД ПУЕТ: збірка React-інтерфейсу з modules/sed-frontend + nginx, що проксіює /api на застосунок.
# Node.js на сервері не потрібен: інтерфейс збирається всередині першої стадії образу.
FROM node:22-alpine AS build
WORKDIR /src
COPY modules/sed-frontend/package.json modules/sed-frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY modules/sed-frontend/ ./
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.27-alpine
ENV SED_API_UPSTREAM=http://app:8080 KEYCLOAK_ORIGIN=http://localhost:8180
COPY deploy/nginx/default.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /src/dist/ /usr/share/nginx/html/
EXPOSE 8080
