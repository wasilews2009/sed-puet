# Збірка інтерфейсу і віддача через nginx (окремий контейнер перед sed-app).
# Альтернатива без цього контейнера: SED_APP_WEB_ROOT=/шлях/до/dist — тоді index.html і /static/ віддає сам sed-app.
FROM node:22-alpine AS build
WORKDIR /src
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.27-alpine
# KEYCLOAK_ORIGIN (напр. https://sso.puet.edu.ua) потрапляє в connect-src CSP; SED_API_UPSTREAM — адреса sed-app
ENV SED_API_UPSTREAM=http://app:8080 KEYCLOAK_ORIGIN=http://localhost:8180
COPY deploy/nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /src/dist /usr/share/nginx/html
EXPOSE 8080
