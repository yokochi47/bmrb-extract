#!/bin/bash
#
# Create the 'internal' (service) and 'prefect' databases and the service schema.
#
# The official entrypoint runs this ONLY when the data directory is empty, which
# is why the schema-shaped settings (SERVICE_DOMAIN, the database names) are
# first-start-only. Everything else in .env is re-read on every restart.

set -euo pipefail

src=/opt/bmrb-extract
rendered=/tmp/bmrb-extract-init.sql

: "${POSTGRES_SERVICE_DB:?set POSTGRES_SERVICE_DB in .env}"
: "${POSTGRES_PREFECT_DB:?set POSTGRES_PREFECT_DB in .env}"
: "${SERVICE_DOMAIN:?set SERVICE_DOMAIN in .env}"

# Same three fragments, in the same order, as upstream's config.sh.
cat "$src/setup-service.sql.template" \
    "$src/init-service.sql.template" \
    "$src/init-prefect.sql.template" \
  | envsubst > "$rendered"

# session.processing_site is an enum of the two upstream production sites. A
# local instance running under any other domain needs its value added, or every
# insert fails on the column default.
case "$SERVICE_DOMAIN" in
    bmrb.io|pdbj.org)
        ;;
    *)
        echo "bmrb-extract: adding '$SERVICE_DOMAIN' to the processing_site_code enum"
        sed -i \
            "s/AS ENUM ('bmrb.io', 'pdbj.org')/AS ENUM ('bmrb.io', 'pdbj.org', '${SERVICE_DOMAIN}')/" \
            "$rendered"
        grep -q "'${SERVICE_DOMAIN}'" "$rendered" || {
            echo "bmrb-extract: could not extend processing_site_code — the upstream" >&2
            echo "  enum declaration in postgres/init-service.sql.template changed shape." >&2
            exit 1
        }
        ;;
esac

# The templates use psql meta-commands (\gexec, \c), so they must go through
# psql -f rather than the entrypoint's plain SQL handling.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -f "$rendered"

rm -f "$rendered"

echo "bmrb-extract: initialised '$POSTGRES_SERVICE_DB' and '$POSTGRES_PREFECT_DB'"
