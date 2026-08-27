#!/bin/sh
#
# Backend container start: render the site configuration from the environment,
# make sure the storage trees exist, then hand over to Gunicorn.

set -eu

preflight-storage "${ARCHIVE_BASE_PATH}" "${WORKSPACE_BASE_PATH}"

render-site-config /app/core/site_config.py

# The archive is a git repository per session. Bind-mounted directories can be
# owned by a different uid than this container's, which trips git's
# dubious-ownership check; the archive is ours by construction.
# (HOME is set to a writable directory in the image, so --global works even
# though this runs as a non-root user.)
git config --global --add safe.directory '*'
git config --global init.defaultBranch main

exec "$@"
