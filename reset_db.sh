#!/bin/bash

set -eu

if [[ ! -e .env ]] ; then
  echo "Error: Missing .env file. Run ./config.sh first."
  exit 1
fi

source .env

if [[ "${SERVICE_LEVEL}" != "development" ]] ; then
  echo "Error: reset_db.sh is only allowed in development (current: ${SERVICE_LEVEL})."
  exit 1
fi

if [[ ! -e postgres/reset.sql ]] ; then
  echo "Error: Missing postgres/reset.sql. Run ./config.sh first."
  exit 1
fi

read -p "Drop and recreate the '${POSTGRES_SERVICE_DB}' schema? [y/N] " ans
[[ "${ans}" =~ ^[Yy] ]] || exit 0

docker exec -i bmrb-extract-postgres \
  psql -U "${POSTGRES_USER}" -d "${POSTGRES_SERVICE_DB}" < postgres/reset.sql

echo "Development database reset."

