#!/bin/bash
# Bring upload_file_type on a live database in line with init-service.sql.template
# (idempotent). init.sql only runs on a fresh volume, so live sites need this by hand.
#
#  1. Add values introduced after the initial schema ('nm-shi-dia').
#  2. Drop the mistakenly added 'nm-shi-vie' / 'nm-csp-vie' if present. PostgreSQL
#     cannot drop an enum value, so the type is rebuilt without them; rows that
#     still carry one are first remapped ('nm-shi-vie' -> 'nm-shi-pip',
#     'nm-csp-vie' -> 'nm-csp-pip'). Past runs' manifest.json files in the git
#     archive are left as they were.

set -eu

cd "$(dirname "$0")/.."

if [[ ! -e .env ]] ; then
  echo "Error: Missing .env file. Run ./config.sh first."
  exit 1
fi

source .env

# ADD VALUE gets its own transaction so the rebuild below sees the new label.
docker exec -i bmrb-extract-postgres \
  psql -v ON_ERROR_STOP=1 -U "${POSTGRES_USER}" -d "${POSTGRES_SERVICE_DB}" <<'SQL'
ALTER TYPE upload_file_type ADD VALUE IF NOT EXISTS 'nm-shi-dia' BEFORE 'nm-shi-gar';
SQL

docker exec -i bmrb-extract-postgres \
  psql -v ON_ERROR_STOP=1 -U "${POSTGRES_USER}" -d "${POSTGRES_SERVICE_DB}" <<'SQL'
BEGIN;
DO $$
DECLARE
  labels text;
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_enum
     WHERE enumtypid = 'upload_file_type'::regtype
       AND enumlabel IN ('nm-shi-vie', 'nm-csp-vie')
  ) THEN
    RAISE NOTICE 'upload_file_type: no nm-shi-vie / nm-csp-vie to drop';
    RETURN;
  END IF;

  UPDATE upload_file SET file_type = 'nm-shi-pip' WHERE file_type::text = 'nm-shi-vie';
  UPDATE upload_file SET file_type = 'nm-csp-pip' WHERE file_type::text = 'nm-csp-vie';

  -- Keep every other label in its current order.
  SELECT string_agg(quote_literal(enumlabel), ', ' ORDER BY enumsortorder)
    INTO labels
    FROM pg_enum
   WHERE enumtypid = 'upload_file_type'::regtype
     AND enumlabel NOT IN ('nm-shi-vie', 'nm-csp-vie');

  ALTER TYPE upload_file_type RENAME TO upload_file_type_old;
  EXECUTE 'CREATE TYPE upload_file_type AS ENUM (' || labels || ')';
  ALTER TABLE upload_file
    ALTER COLUMN file_type TYPE upload_file_type USING file_type::text::upload_file_type;
  DROP TYPE upload_file_type_old;

  RAISE NOTICE 'upload_file_type: dropped nm-shi-vie / nm-csp-vie';
END
$$;
COMMIT;

SELECT enumlabel FROM pg_enum
 WHERE enumtypid = 'upload_file_type'::regtype
   AND enumlabel ~ '^nm-(shi|csp)-(vie|dia|pip)$'
 ORDER BY enumsortorder;
SQL
