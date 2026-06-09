#!/bin/bash

set -eu

# After running nginx, frontend is no longer needed.
( sleep 60 ; [[ `docker compose ps frontend &> /dev/null` ]] && docker compose down frontend && yes | docker image prune ) &

docker compose down nginx
docker compose build frontend
docker compose build nginx
docker compose up nginx --remove-orphans

