# cFlow web (Studio / Model / Portal): Vite build served by nginx (COOP/COEP + /api/v1 proxy).
#
#   docker build -t ghcr.io/fernandomarcius/cflow-web web
FROM node:22-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . ./
RUN npm run build

FROM nginx:alpine
# Where /api goes and who resolves the name (see nginx.conf). Defaults match docker compose; in
# Kubernetes override them through the service environment variables.
ENV CFLOW_API_UPSTREAM=http://api:8000 \
    CFLOW_DNS_RESOLVER=127.0.0.11
# templates/*.template -> envsubst at startup -> conf.d/ (official nginx image entrypoint).
COPY nginx.conf /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
