# Flask/Gunicorn API for the local deployment.
#
# Differs from backend/Dockerfile in two ways: the build context is the repo
# root (so the shared upstream templates can be copied in), and the application
# source is BAKED IN rather than bind-mounted, so the running service is exactly
# what was built. core/site_config.py is rendered by the entrypoint from the
# environment on every start.

FROM python:3.12-alpine

ENV PIP_ROOT_USER_ACTION=ignore \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# gcc/musl-dev/libffi-dev build the wheels that have no musl binaries;
# git backs the per-session upload archive (GitPython);
# gettext provides envsubst for the site-config renderer.
RUN apk add --no-cache gcc musl-dev libffi-dev git gettext

WORKDIR /app

COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application source. .dockerignore keeps any locally-rendered site_config.py
# out of the context so it cannot shadow the one the entrypoint writes.
COPY backend/app/ /app/

# Upstream's site-config template, rendered at start rather than at build.
COPY backend/app/core/site_config.py.template /opt/bmrb-extract/site_config.py.template

COPY docker/common/render-site-config.sh /usr/local/bin/render-site-config
COPY docker/common/preflight-storage.sh /usr/local/bin/preflight-storage
COPY docker/backend/entrypoint.sh /usr/local/bin/entrypoint
RUN chmod +x /usr/local/bin/render-site-config /usr/local/bin/preflight-storage \
             /usr/local/bin/entrypoint

# Run as the invoking user rather than root, so files written to a shared
# filesystem belong to them. On NFS with the usual root_squash export, a
# root container is mapped to `nobody` and cannot write into their directories
# at all, so this is not merely tidiness.
#
# The account is created rather than just passed via `user:` because tools that
# look up their own passwd entry fail on a uid that does not exist. Defaults to
# 0:0, which keeps the image root-based when the args are not supplied.
ARG RUNTIME_UID=0
ARG RUNTIME_GID=0
RUN set -eu; \
    if [ "$RUNTIME_UID" != "0" ]; then \
        if ! getent group "$RUNTIME_GID" >/dev/null; then \
            addgroup -g "$RUNTIME_GID" appgrp; \
        fi; \
        grp="$(getent group "$RUNTIME_GID" | cut -d: -f1)"; \
        if ! getent passwd "$RUNTIME_UID" >/dev/null; then \
            adduser -D -H -u "$RUNTIME_UID" -G "$grp" -h /home/app appuser; \
        fi; \
    fi; \
    mkdir -p /home/app; \
    chown "$RUNTIME_UID:$RUNTIME_GID" /home/app
ENV HOME=/home/app

# The entrypoint writes core/site_config.py here on every start.
RUN chown -R "$RUNTIME_UID:$RUNTIME_GID" /app

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/entrypoint"]
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "app:app"]
