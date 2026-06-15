# Prefect worker image with the Docker CLI added.
#
# The process_session flow runs the maxit-ccd tool as an ephemeral container
# (docker run, with a per-conversion memory limit, per the maxit-ccd README).
# The worker talks to the host Docker daemon through the mounted socket
# (/var/run/docker.sock), so only the Docker *client* is needed here.
FROM prefecthq/prefect:3-latest

# docker-cli provides just the `docker` client binary (no daemon); the daemon is
# the host's, reached via the mounted socket. (The docker.io package keeps the
# CLI in a separate `docker-cli` recommends, so install docker-cli directly.)
RUN apt-get update \
    && apt-get install -y --no-install-recommends docker-cli \
    && rm -rf /var/lib/apt/lists/*
