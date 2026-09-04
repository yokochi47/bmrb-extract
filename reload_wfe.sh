#!/bin/bash

set -eu

#
# Verify frontend version before deploy
#
frontend_version=$(grep -m1 '"version"' frontend/package.json \
  | sed -E 's/.*"version"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/')
FRONTEND_VERSION=$(grep -m1 'FRONTEND_VERSION' frontend/src/site.config.ts \
  | sed -E "s/.*=[[:space:]]'([^']+)'.*/\1/")

if [[ ${FRONTEND_VERSION} != ${frontend_version} ]] ; then

  export FRONTEND_VERSION=${frontend_version}

  if [[ ${SERVICE_DOMAIN} = "bmrb.io" ]] ; then

    ( cd frontend/src
      rm -f index.html site.config.ts
      ln -s index.bmrb.html index.html
      envsubst < bmrb.config.ts.template > bmrb.config.ts
      ln -s bmrb.config.ts site.config.ts )

  elif [[ "${SERVICE_DOMAIN}" = "pdbj.org" ]] ; then

    ( cd frontend/src
      rm -f index.html site.config.ts
      ln -s index.bmrbj.html index.html
      envsubst < bmrbj.config.ts.template > bmrbj.config.ts
      ln -s bmrbj.config.ts site.config.ts )

  fi

fi

# After running nginx, frontend is no longer needed.
( sleep 60 ; [[ `docker compose ps frontend &> /dev/null` ]] && docker compose down frontend && yes | docker image prune ) &

docker compose down nginx
docker compose build frontend
docker compose build nginx
# Detached: nginx serves the rebuilt frontend in the background and the script
# returns cleanly. (Previously this ran attached/foreground, which had to be
# manually killed to end the script — an error-prone step.)
docker compose up -d nginx --remove-orphans

