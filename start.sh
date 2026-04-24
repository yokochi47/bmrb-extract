#!/bin/bash

set -eu

# After running nginx, frontend is no longer needed.
( sleep 120 ; docker compose down frontend ) &

docker compose up -d --remove-orphans

# Prune unused images
yes | docker image prune

# Prune unused volumes
yes | docker volume prune

