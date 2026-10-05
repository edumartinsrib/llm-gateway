# syntax=docker/dockerfile:1

# ---------------------------------------------------------------- build
FROM node:20-alpine AS build
WORKDIR /app

# A broto-ui vem empacotada em vendor/ até ser publicada (ver README).
COPY package.json package-lock.json ./
COPY vendor ./vendor
RUN npm ci --no-audit --no-fund

COPY tsconfig.json tsconfig.app.json tsconfig.node.json vite.config.ts index.html ./
COPY public ./public
COPY src ./src
RUN npm run build

# ---------------------------------------------------------------- runtime
FROM nginx:1.27-alpine

# Upstream resolvido pelo envsubst nativo da imagem (templates em /etc/nginx/templates).
ENV LLMGW_API_UPSTREAM=http://gateway-api:8000 \
    NGINX_ENVSUBST_FILTER=^LLMGW_

COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s CMD wget -qO- http://127.0.0.1:8080/healthz >/dev/null || exit 1
