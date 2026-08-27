# PostgreSQL for the local deployment.
#
# Upstream renders postgres/init.sql on the host with config.sh and bind-mounts
# it. This image carries the upstream .sql.template files and assembles them
# with envsubst inside the container on first start, so no host step is needed
# and the values come from .env.
#
# v18 is required: the schema uses the built-in uuidv7().

FROM postgres:18-alpine

# envsubst
RUN apk add --no-cache gettext

COPY postgres/setup-service.sql.template \
     postgres/init-service.sql.template \
     postgres/init-prefect.sql.template \
     /opt/bmrb-extract/

COPY docker/postgres/initdb/10-bmrb-extract.sh /docker-entrypoint-initdb.d/10-bmrb-extract.sh
RUN chmod +x /docker-entrypoint-initdb.d/10-bmrb-extract.sh
