#!/bin/bash

# After running nginx, frontend is no longer needed.
(sleep 120 ; docker compose down frontend) &

docker compose up -d

# Prune unused images
# docker image prune

# Prune unused volumes
# docker volume prune

