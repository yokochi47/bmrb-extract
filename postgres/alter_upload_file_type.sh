#!/bin/bash
# Add upload_file_type values introduced after the initial schema (idempotent).
# init.sql only runs on a fresh volume, so live sites need this applied by hand.

set -eu

cd "$(dirname "$0")/.."

if [[ ! -e .env ]] ; then
  echo "Error: Missing .env file. Run ./config.sh first."
  exit 1
fi

source .env

docker exec -i bmrb-extract-postgres \
  psql -v ON_ERROR_STOP=1 -U "${POSTGRES_USER}" -d "${POSTGRES_SERVICE_DB}" <<'SQL'
ALTER TYPE upload_file_type ADD VALUE IF NOT EXISTS 'nm-csp-vie' BEFORE 'nm-csp-xea';
ALTER TYPE upload_file_type ADD VALUE IF NOT EXISTS 'nm-shi-vie' BEFORE 'nm-shi-xea';
SELECT enumlabel FROM pg_enum
 WHERE enumtypid = 'upload_file_type'::regtype AND enumlabel LIKE 'nm-%-vie'
 ORDER BY enumsortorder;
SQL
