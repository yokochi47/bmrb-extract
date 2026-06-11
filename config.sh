#!/bin/bash

set -eu

source .env.template

if [ -e .env ] ; then
  source .env
fi

echo "Choose service level: [ production or development ]"

read ans

case "${ans}" in
  p*)
    SERVICE_LEVEL=production
    ;;
  d*)
    SERVICE_LEVEL=development
    ;;
  *)
    echo "Error: ${ans} is not defined."
    exit 1
    ;;

esac

if [[ -z "${SERVICE_DOMAIN}" ]] ; then

  echo "Choose service domain: [ bmrb.io or pdbj.org ]"

  read ans

  case "${ans}" in
    b*)
      SERVICE_DOMAIN=bmrb.io
      SERVICE_HELP_EMAIL=help@bmrb.io
      CONV_ID_RANGE_BEGIN=1000001
      CONV_ID_RANGE_END=2000000
      TZ=US/Eastern
      ;;
    p*)
      SERVICE_DOMAIN=pdbj.org
      SERVICE_HELP_EMAIL=bmrbhelp@protein.osaka-u.ac.jp
      CONV_ID_RANGE_BEGIN=2000001
      CONV_ID_RANGE_END=3000000
      TZ=Asia/Tokyo
      ;;
    *)
      echo "Error: ${ans} is not defined."
      exit 1
      ;;
  esac

elif [[ "${SERVICE_DOMAIN}" = "bmrb.io" ]] ; then
  CONV_ID_RANGE_BEGIN=1000001
  CONV_ID_RANGE_END=2000000
  TZ=US/Eastern
elif [[ "${SERVICE_DOMAIN}" = "pdbj.org" ]] ; then
  CONV_ID_RANGE_BEGIN=2000001
  CONV_ID_RANGE_END=3000000
  TZ=Asia/Tokyo
fi

if [[ "${SERVICE_LEVEL}" = "development" ]] ; then
  CONV_ID_RANGE_BEGIN=8000001
  CONV_ID_RANGE_END=9000000
fi

email_regex='^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\n$'

if [[ -z "${SERVICE_ADMIN_EMAIL}" ]] ; then

  echo "Enter administrator e-mail address:"

  read ans

  if [[ "${ans}" =~ $email_regex ]] ; then
    echo "Error: ${ans} is not valid."
    exit 1
  fi

  SERVICE_ADMIN_EMAIL=$ans

fi

if [[ -z "${NGINX_LOG_FORMAT}" ]] ; then

  echo "Choose nginx log format: [ default or json ]"

  read ans

  case "${ans}" in
    j*)
      NGINX_LOG_FORMAT=json
      ;;
    *)
      NGINX_LOG_FORMAT=default
      ;;
  esac

fi

if [[ -z "${POSTGRES_PASSWORD}" ]] ; then

  echo "Enter password for PostgreSQL user (${POSTGRES_USER})"

  read ans

  case "${ans}" in
    *[[:space:]]*)
      echo "Error: ${ans} contains whitespace characters."
      exit 1
      ;;
    *[\t]*)
      echo "Error: ${ans} contains a tab character."
      exit 1
      ;;
    *)
      POSTGRES_PASSWORD=$ans
      ;;
  esac

fi

token_regex='^[0-9A-Z]{29}\n$'

if [[ -z "${MAXIT_CCD_SELF_RUNNER_TOKEN}" ]] ; then


  echo "Enter GitHub Action Runner token of ${MAXIT_CCD_REPO} repositor: [29 charactors string]"

  read ans

  if [[ "${ans}" =~ $token_regex ]] ; then
    echo "Error: ${ans} is not valid."
    exit 1
  fi

  MAXIT_CCD_SELF_RUNNER_TOKEN=$ans

fi

if [[ -z "${UTILS_NMR_SELF_RUNNER_TOKEN}" ]] ; then

  echo "Enter GitHub Action Runner token of ${UTILS_NMR_REPO} repositor: [29 charactors string]"

  read ans

  if [[ "${ans}" =~ $token_regex ]] ; then
    echo "Error: ${ans} is not valid."
    exit 1
  fi

  UTILS_NMR_SELF_RUNNER_TOKEN=$ans

fi

if [[ -z "${SECRET_KEY}" ]] ; then

  SECRET_KEY=$(python3 -c "import uuid; print(uuid.uuid4())")

fi

grep -v '=\s' .env.template | grep -v SERVICE_HOST > .env

cat << EOF >> .env
##
## Configure bellow lines
##
export SECRET_KEY=${SECRET_KEY}
export SMTP_SERVER=${SMTP_SERVER}
export CONV_ID_RANGE_BEGIN=${CONV_ID_RANGE_BEGIN}
export CONV_ID_RANGE_END=${CONV_ID_RANGE_END}
export TZ=${TZ}

# Standalone NMR data conversion service
export SERVICE_LEVEL=${SERVICE_LEVEL}
export SERVICE_DOMAIN=${SERVICE_DOMAIN}
export SERVICE_HOST=${SERVICE_SUBDOMAIN}.${SERVICE_DOMAIN}
export SERVICE_ADMIN_EMAIL=${SERVICE_ADMIN_EMAIL}
export SERVICE_HELP_EMAIL=${SERVICE_HELP_EMAIL}

# Nginx
NGINX_LOG_FORMAT=${NGINX_LOG_FORMAT}

# Backend (Flask)
FLASK_ENV=${SERVICE_LEVEL}
export FLASK_API_URL=http://backend:8000/api/

# PostgreSQL
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
# Data volume isolated per service level (overrides .env.template defaults)
POSTGRES_DATA_VOL_LABEL=pg_data_${SERVICE_LEVEL}
POSTGRES_DATA_VOL_DIR=/var/lib/pg_data_${SERVICE_LEVEL}
export SERVICE_DATABASE_URL=postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:5432/${POSTGRES_SERVICE_DB}
PREFECT_API_DATABASE_CONNECTION_URL=postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:5432/${POSTGRES_PREFECT_DB}

# GitHub Container Repository
MAXIT_CCD_SELF_RUNNER_TOKEN=${MAXIT_CCD_SELF_RUNNER_TOKEN}
UTILS_NMR_SELF_RUNNER_TOKEN=${UTILS_NMR_SELF_RUNNER_TOKEN}

EOF

check_file () {

  if [[ ! -e ${1} ]] || [[ -z ${1} ]] ; then
    echo Failed to generate ${1} file.
    exit 1
  fi

  echo Generated ${1} file.

}

check_file .env

source .env

#
# Write nginx.conf
#
( cd nginx
  rm -f nginx.conf.template
  ln -s nginx-${SERVICE_LEVEL}.conf.template nginx.conf.template )
sed -e 's/${SERVICE_HOST}/'"${SERVICE_HOST}"'/' < nginx/nginx.conf.template | \
sed -e 's/${NGINX_LOG_FORMAT}/'"${NGINX_LOG_FORMAT}"'/' > nginx/nginx.conf

check_file nginx/nginx.conf

#
# Write ssl.conf
#
envsubst < nginx/ssl.conf.template > nginx/ssl.conf

check_file nginx/ssl.conf

#
# Write init.sql
#
( cd postgres
  rm -f init.sql.template
  cat setup-service.sql.template > init.sql.template
  cat init-service.sql.template >> init.sql.template
  cat init-prefect.sql.template >> init.sql.template )
envsubst < postgres/init.sql.template > postgres/init.sql

check_file postgres/init.sql

#
# Write reset.sql (development only; used by reset_db.sh)
#
if [[ "${SERVICE_LEVEL}" = "development" ]] ; then
  ( cd postgres
    cat reset-service.sql.template > reset.sql.tmp.template
    cat init-service.sql.template >> reset.sql.tmp.template )
  envsubst < postgres/reset.sql.tmp.template > postgres/reset.sql
  rm -f postgres/reset.sql.tmp.template

  check_file postgres/reset.sql
fi

#
# Write certbot.sh
#
envsubst < certbot/certbot.sh.template > certbot/certbot.sh

check_file certbot/certbot.sh

#
# Frontend
#
if [[ ${SERVICE_DOMAIN} = "bmrb.io" ]] ; then

  ( cd frontend/src
    rm -f index.html site.config.ts
    ln -s index.bmrb.html index.html
    envsubst < bmrb.config.ts.template > bmrb.config.ts
    ln -s bmrb.config.ts site.config.ts )

else

  ( cd frontend/src
    rm -f index.html site.config.ts
    ln -s index.bmrbj.html index.html
    envsubst < bmrbj.config.ts.template > bmrbj.config.ts
    ln -s bmrbj.config.ts site.config.ts )

fi

check_file frontend/src/index.html
check_file frontend/src/site.config.ts

#
# Backend
#
envsubst < backend/app/core/site_config.py.template > backend/app/core/site_config.py

check_file backend/app/core/site_config.py

