#!/bin/sh
#
# Render the bmrb-extract server block from the environment.
#
# Kept out of /etc/nginx/templates so the stock 20-envsubst-on-templates.sh does
# not touch it: that script substitutes every $name it finds, which would eat
# nginx's own $remote_addr, $host and friends. Here the substituted set is
# explicit.

set -eu

template=/etc/nginx/templates.local/bmrb-extract.conf.template
output=/etc/nginx/conf.d/bmrb-extract.conf

# .env speaks upstream's vocabulary ("default" / "json"); the config block
# defines the formats under prefixed names so they cannot clash with the ones
# the stock nginx.conf declares.
case "${NGINX_LOG_FORMAT:-default}" in
    json) NGINX_LOG_FORMAT=bmrbx_json ;;
    *)    NGINX_LOG_FORMAT=bmrbx_default ;;
esac
export NGINX_LOG_FORMAT

export SERVICE_HOST="${SERVICE_HOST:-_}"
export NGINX_CLIENT_MAX_BODY_SIZE="${NGINX_CLIENT_MAX_BODY_SIZE:-300m}"
export NGINX_PROXY_READ_TIMEOUT="${NGINX_PROXY_READ_TIMEOUT:-600s}"

envsubst '${SERVICE_HOST} ${NGINX_CLIENT_MAX_BODY_SIZE} ${NGINX_LOG_FORMAT} ${NGINX_PROXY_READ_TIMEOUT}' \
    < "$template" > "$output"

echo "bmrb-extract: rendered ${output} (server_name ${SERVICE_HOST}, log ${NGINX_LOG_FORMAT})"
