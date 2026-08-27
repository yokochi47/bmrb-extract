# PostgreSQL for the local deployment.
#
# Upstream renders postgres/init.sql on the host with config.sh and bind-mounts
# it. This image carries the upstream .sql.template files and assembles them
# with envsubst inside the container on first start, so no host step is needed
# and the values come from .env.
#
# v18 is required: the schema uses the built-in uuidv7().

FROM postgres:18-alpine

# envsubst
RUN apk add --no-cache gettext

COPY postgres/setup-service.sql.template \
     postgres/init-service.sql.template \
     postgres/init-prefect.sql.template \
     /opt/bmrb-extract/

COPY docker/postgres/initdb/10-bmrb-extract.sh /docker-entrypoint-initdb.d/10-bmrb-extract.sh
RUN chmod +x /docker-entrypoint-initdb.d/10-bmrb-extract.sh

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

# initdb resolves its own passwd entry (get_user_name) and aborts on a uid that
# does not exist, which is why the account above is created and not just
# selected with `user:`.
