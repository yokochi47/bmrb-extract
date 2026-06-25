#!/bin/bash

set -eu

# After running nginx, frontend is no longer needed.
( sleep 120 ; docker compose down frontend ) &

docker compose up -d --remove-orphans

# Register the Prefect deployment (process-session/default) so POST /api/process
# can trigger flow runs. Idempotent — re-running updates the existing deployment.
echo "Waiting for the Prefect server to become reachable..."
for _ in $(seq 1 60); do
  docker compose exec -T prefect-worker \
    python -c "import urllib.request as u; u.urlopen('http://prefect-server:4200/api/health', timeout=2)" \
    2>/dev/null && break
  sleep 2
done
docker compose exec -T prefect-worker sh -c "cd /flows && prefect deploy --all" \
  || echo "WARNING: 'prefect deploy' failed; /api/process cannot trigger runs until the deployment is registered."

# Seed /workspace/versions.json once now (the capture-versions deployment also
# runs on a schedule) so the footer has version data before the first tick.
docker compose exec -T prefect-worker sh -c "cd /flows && prefect deployment run 'capture-versions/default'" \
  || echo "WARNING: failed to seed versions.json; the footer versions appear after the first scheduled capture."

# Prune unused images
yes | docker image prune

# Prune unused volumes
yes | docker volume prune

