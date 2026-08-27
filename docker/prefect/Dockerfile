# Prefect worker for the local deployment.
#
# Carries the flows and the shared service package in the image instead of
# bind-mounting them, and registers the deployments itself on start — the job
# upstream's start.sh does from the host after `docker compose up`.

FROM prefecthq/prefect:3-latest

# docker-cli is the client only; the daemon is the host's, reached through the
#   mounted socket. The conversion tasks run maxit-ccd, py-wwpdb_utils_nmr and
#   the PDF generator as sibling containers.
# openssh-client + rsync serve the cross-site exchange flow.
# gettext-base provides envsubst for the site-config renderer.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        docker-cli openssh-client rsync gettext-base git \
    && rm -rf /var/lib/apt/lists/*

# Flows, and the shared service package they reach through the
# prefect/flows/shared/core symlink (-> ../../../backend/app/core, which
# resolves to /backend/app/core inside the image). COPY preserves the symlink,
# so this layout has to match.
COPY prefect/flows/ /flows/
COPY backend/app/ /backend/app/

# The flows import the same SQLAlchemy models and site config as the backend.
# Unpinned and without -U, so pip leaves whatever Prefect already pins in place
# and only fills in what is missing (asyncpg, in practice). gitpython is here
# because the archive the flows read is a git repository per session.
RUN pip install --no-cache-dir \
        "SQLAlchemy[asyncio]" asyncpg gitpython

COPY backend/app/core/site_config.py.template /opt/bmrb-extract/site_config.py.template
COPY docker/common/render-site-config.sh /usr/local/bin/render-site-config
COPY docker/common/preflight-storage.sh /usr/local/bin/preflight-storage
COPY docker/prefect/entrypoint.sh /usr/local/bin/entrypoint
RUN chmod +x /usr/local/bin/render-site-config /usr/local/bin/preflight-storage \
             /usr/local/bin/entrypoint

# Run as the invoking user rather than root, so the archive and workspace files
# the flows write belong to them. On NFS with the usual root_squash export a
# root container is mapped to `nobody` and cannot write there at all.
#
# It also fixes the sibling containers: the conversion tasks launch maxit-ccd,
# py-wwpdb_utils_nmr and the PDF generator with `-u $(os.getuid())`, which is
# whatever THIS container runs as. As root that puts root-owned files in the
# archive; as the invoking user it is correct by construction.
#
# The account is created rather than just selected with `user:` so anything
# resolving its own passwd entry does not fail on an unknown uid. Debian's
# useradd here, Alpine's adduser in the other images.
ARG RUNTIME_UID=0
ARG RUNTIME_GID=0
RUN set -eu; \
    if [ "$RUNTIME_UID" != "0" ]; then \
        getent group "$RUNTIME_GID" >/dev/null || groupadd -g "$RUNTIME_GID" appgrp; \
        getent passwd "$RUNTIME_UID" >/dev/null \
            || useradd -u "$RUNTIME_UID" -g "$RUNTIME_GID" -M -d /home/app -s /bin/sh appuser; \
    fi; \
    mkdir -p /home/app; \
    chown "$RUNTIME_UID:$RUNTIME_GID" /home/app

# HOME must be writable: the entrypoint runs `git config --global`, and Prefect
# falls back to ~/.prefect when PREFECT_HOME is unset. /root is not writable by
# a non-root user.
ENV HOME=/home/app

# The entrypoint renders core/site_config.py into the shared package on every
# start, so that tree has to be writable by the runtime user.
RUN chown -R "$RUNTIME_UID:$RUNTIME_GID" /backend/app /flows

WORKDIR /flows

ENTRYPOINT ["/usr/local/bin/entrypoint"]
