#!/bin/bash
# Add the help_attachment table (help-desk file attachments) to a live database
# (idempotent). init.sql only runs on a fresh volume, so live sites need this by hand.
# Keep the DDL in step with postgres/init-service.sql.template.

set -eu

cd "$(dirname "$0")/.."

if [[ ! -e .env ]] ; then
  echo "Error: Missing .env file. Run ./config.sh first."
  exit 1
fi

source .env

docker exec -i bmrb-extract-postgres \
  psql -v ON_ERROR_STOP=1 -U "${POSTGRES_USER}" -d "${POSTGRES_SERVICE_DB}" <<'SQL'
BEGIN;
CREATE TABLE IF NOT EXISTS help_attachment (
    conversion_id   INT,
    comm_ordinal    INT,
    ordinal         INT CHECK ( ordinal > 0 ),

    original_name   TEXT NOT NULL,
    stored_path     TEXT NOT NULL,
    file_size       BIGINT NOT NULL,
    sha256          TEXT NOT NULL,

    uploaded_at     TIMESTAMP NOT NULL DEFAULT now(),
    expires_at      TIMESTAMP NOT NULL,
    expired_at      TIMESTAMP,

    PRIMARY KEY ( conversion_id, comm_ordinal, ordinal ),
    FOREIGN KEY ( conversion_id, comm_ordinal )
        REFERENCES communication ( conversion_id, ordinal ) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS help_attachment_live_idx ON help_attachment ( expires_at ) WHERE expired_at IS NULL;
COMMIT;
SQL

echo "help_attachment is in place."
