#!/bin/sh
#
# Publish the Angular bundle and inject the site configuration into it.
#
# The bundle is compiled once at image build time with placeholders where the
# site-specific values go (docker/frontend/site.config.local.ts). Here we copy
# the pristine build into the served directory and substitute the current
# environment into it — so the values change on restart without a rebuild.
#
# The copy matters: substituting in place would consume the placeholders on the
# first start and leave nothing to substitute on the next one.

set -eu

pristine=/opt/bmrb-extract/frontend
served=/usr/share/nginx/html

echo "bmrb-extract: publishing frontend to ${served}"

rm -rf "${served:?}"/*
cp -a "$pristine"/. "$served"/
cp -a /opt/bmrb-extract/share/401.html /opt/bmrb-extract/share/403.html "$served"/

version="${FRONTEND_VERSION:-$(cat /opt/bmrb-extract/frontend-version)}"

# One JSON blob, base64-encoded so the payload cannot contain a character that
# would terminate the JavaScript string literal it is substituted into.
payload=$(
    jq -nc \
        --arg HOST_SITE_NAME                    "${HOST_SITE_NAME:-}" \
        --arg HOST_SITE_LOGO                    "${HOST_SITE_LOGO:-}" \
        --arg HOST_SITE_URL                     "${HOST_SITE_URL:-}" \
        --arg ONEDEP_URL                        "${ONEDEP_URL:-}" \
        --arg BMRBDEP_URL                       "${BMRBDEP_URL:-}" \
        --arg SERVICE_HELP_EMAIL                "${SERVICE_HELP_EMAIL:-}" \
        --arg SUCCESS_VALIDITY_PERIOD_IN_DAYS   "${SUCCESS_VALIDITY_PERIOD_IN_DAYS:-}" \
        --arg FAILURE_VALIDITY_PERIOD_IN_DAYS   "${FAILURE_VALIDITY_PERIOD_IN_DAYS:-}" \
        --arg FRONTEND_VERSION                  "$version" \
        --arg SERVICE_LEVEL                     "${SERVICE_LEVEL:-}" \
        '$ARGS.named' \
    | tr -d '\n' | base64 | tr -d '\n'
)

# base64 output contains no '|', '&' or backslash, so it is safe as a sed
# replacement with '|' as the delimiter.
find "$served" -type f \( -name '*.js' -o -name '*.mjs' \) -exec \
    sed -i "s|@@BMRBX_SITE_CONFIG_B64@@|${payload}|g" {} +

sed -i \
    -e "s|@@BMRBX_PAGE_TITLE@@|${PAGE_TITLE:-bmrb_extract}|g" \
    -e "s|@@BMRBX_FAVICON@@|${HOST_SITE_FAVICON:-favicon.bmrb.ico}|g" \
    "$served/index.html"

if grep -rqs '@@BMRBX_' "$served"; then
    echo "bmrb-extract: WARNING — unsubstituted @@BMRBX_ placeholders remain:" >&2
    grep -rlso '@@BMRBX_[A-Z_]*@@' "$served" >&2 || true
fi

echo "bmrb-extract: frontend published (version ${version})"
