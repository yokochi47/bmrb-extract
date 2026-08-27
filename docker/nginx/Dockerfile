# Web tier for the local deployment: the Angular bundle plus the reverse proxy.
#
# Upstream splits this in two — a `frontend` container that builds the bundle
# and idles so nginx can `COPY --from=bmrb-extract-frontend:latest`, which means
# the nginx build silently depends on an image an earlier command produced. Here
# it is one multi-stage build, so `docker compose build nginx` is self-contained
# and there is no frontend service to start and shut down again.
#
# TLS is not terminated here (plain HTTP by choice) — put your own reverse proxy
# in front if you need HTTPS.

# Upstream compiles nginx from source to get QUIC/HTTP-3, pinning the version at
# config.sh time by querying GitHub. HTTP/3 needs host sysctl tuning a container
# cannot do, and this deployment serves plain HTTP, so the official image is
# enough — and a version bump is a one-word edit here. Declared before the first
# FROM so it is in the global scope the runtime FROM below can read.
ARG NGINX_VERSION=1.29-alpine

# --- build the Angular application -------------------------------------------
FROM node:24-alpine AS build

WORKDIR /app

COPY frontend/package*.json ./
RUN npm ci || npm install

COPY frontend/ ./

# Upstream symlinks these two per site and renders the config from a template
# before building. We substitute a variant whose values are resolved at runtime
# instead, so nothing site-specific is baked into the bundle.
RUN rm -f src/site.config.ts src/index.html
COPY docker/frontend/site.config.local.ts ./src/site.config.ts
COPY docker/frontend/index.local.html ./src/index.html

RUN npm run build --omit=dev

# The version shown in the footer, so the entrypoint has a default when
# FRONTEND_VERSION is not set in .env.
RUN node -p "require('./package.json').version" > /app/frontend-version

# --- runtime ------------------------------------------------------------------
FROM nginx:${NGINX_VERSION}

# envsubst for the server config; jq builds the frontend config payload.
RUN apk add --no-cache gettext jq

# The stock server block would shadow ours.
RUN rm -f /etc/nginx/conf.d/default.conf

# Pristine copy of the build output. The entrypoint copies it into the served
# directory and substitutes there, so restarting re-applies .env from scratch
# rather than trying to patch already-patched files.
COPY --from=build /app/dist/bmrb_extract/browser /opt/bmrb-extract/frontend
COPY --from=build /app/frontend-version /opt/bmrb-extract/frontend-version

COPY nginx/share/401.html nginx/share/403.html /opt/bmrb-extract/share/

COPY docker/nginx/bmrb-extract.conf.template /etc/nginx/templates.local/bmrb-extract.conf.template

# The stock entrypoint runs everything in /docker-entrypoint.d in order before
# starting nginx, so these need no wrapper.
COPY docker/nginx/entrypoint.d/ /docker-entrypoint.d/
RUN chmod +x /docker-entrypoint.d/*.sh

EXPOSE 80
