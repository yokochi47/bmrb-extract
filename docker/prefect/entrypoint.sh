#!/bin/sh
#
# Prefect worker start: render the site configuration, register the flow
# deployments, then run the worker. Upstream does the registration from the host
# in start.sh; doing it here means `docker compose up -d` is the whole story and
# a schedule change in prefect/flows/prefect.yaml is picked up by a restart.

set -eu

preflight-storage "${ARCHIVE_BASE_PATH}" "${WORKSPACE_BASE_PATH}"

render-site-config /backend/app/core/site_config.py

git config --global --add safe.directory '*'

# The conversion tasks reach the host daemon through the mounted socket. As a
# non-root worker that needs the socket's group (DOCKER_GID in .env); say so
# here rather than letting the first conversion fail with a bare EACCES.
if [ -S /var/run/docker.sock ] && ! docker version >/dev/null 2>&1; then
    echo "WARNING: cannot talk to the Docker daemon as $(id -u):$(id -g)." >&2
    echo "  Conversions will fail. /var/run/docker.sock is owned by group" >&2
    echo "  $(stat -c '%g' /var/run/docker.sock 2>/dev/null || echo '?'); set DOCKER_GID to that in .env." >&2
fi

POOL="${PREFECT_WORK_POOL:-local-pool}"

echo "prefect-worker: waiting for ${PREFECT_API_URL} ..."
i=0
until python -c "import os, urllib.request as u; u.urlopen(os.environ['PREFECT_API_URL'] + '/health', timeout=2)" 2>/dev/null; do
    i=$((i + 1))
    if [ "$i" -ge 90 ]; then
        echo "prefect-worker: Prefect API never became reachable; giving up." >&2
        exit 1
    fi
    sleep 2
done

# The pool must exist before `prefect deploy` can attach deployments to it.
prefect work-pool create --type process "$POOL" 2>/dev/null \
    || echo "prefect-worker: work pool '${POOL}' already exists"

# Idempotent: re-running updates the existing deployments in place.
if (cd /flows && prefect deploy --all); then
    echo "prefect-worker: deployments registered"
else
    echo "prefect-worker: WARNING — 'prefect deploy --all' failed;" >&2
    echo "  POST /api/process cannot trigger conversions until it succeeds." >&2
fi

# Seed /workspace/versions.json so the footer has version data before the first
# scheduled capture (every 600s). Backgrounded: the worker has to be running to
# pick the run up.
(
    sleep 15
    prefect deployment run 'capture-versions/default' >/dev/null 2>&1 \
        || echo "prefect-worker: version seeding skipped (will refresh on schedule)"
) &

exec prefect worker start --pool "$POOL" --type process
