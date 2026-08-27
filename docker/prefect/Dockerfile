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
COPY docker/prefect/entrypoint.sh /usr/local/bin/entrypoint
RUN chmod +x /usr/local/bin/render-site-config /usr/local/bin/entrypoint

WORKDIR /flows

ENTRYPOINT ["/usr/local/bin/entrypoint"]
