#!/bin/bash

set -eu

# After running nginx, frontend is no longer needed.
( sleep 60 ; [[ `docker compose ps frontend &> /dev/null` ]] && docker compose down frontend && yes | docker image prune ) &

docker compose down nginx
docker compose build frontend
docker compose build nginx
# Detached: nginx serves the rebuilt frontend in the background and the script
# returns cleanly. (Previously this ran attached/foreground, which had to be
# manually killed to end the script — an error-prone step.)
docker compose up -d nginx --remove-orphans

