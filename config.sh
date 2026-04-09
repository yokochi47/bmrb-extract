#!/bin/bash

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
			TZ=US/Eastern
			;;
		p*)
			SERVICE_DOMAIN=pdbj.org
			SERVICE_HELP_EMAIL=bmrbhelp@protein.osaka-u.ac.jp
			TZ=Asia/Tokyo
			;;
		*)
			echo "Error: ${ans} is not defined."
			exit 1
			;;
	esac

elif [[ "${SERVICE_DOMAIN}" = "bmrb.io" ]] ; then
	TZ=US/Eastern
elif [[ "${SERVICE_DOMAIN}" = "pdbj.org" ]] ; then
	TZ=Asia/Tokyo
fi

email_regex='^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\n$'

if [[ -z "${SERVICE_ADMIN_EMAIL}" ]] ; then

	echo "Enter administrator e-mail address:"

	read ans

	if [[ "${ans}" =~ $email_regex ]]; then
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

	if [[ "${ans}" =~ $token_regex ]]; then
		echo "Error: ${ans} is not valid."
		exit 1
	fi

	MAXIT_CCD_SELF_RUNNER_TOKEN=$ans

fi

if [[ -z "${UTILS_NMR_SELF_RUNNER_TOKEN}" ]] ; then

	echo "Enter GitHub Action Runner token of ${UTILS_NMR_REPO} repositor: [29 charactors string]"

	read ans

	if [[ "${ans}" =~ $token_regex ]]; then
		echo "Error: ${ans} is not valid."
		exit 1
	fi

	UTILS_NMR_SELF_RUNNER_TOKEN=$ans

fi

grep -v '=\s' .env.template | grep -v SERVICE_HOST > .env

cat << EOF >> .env
##
## Configure bellow lines
##
TZ=${TZ}

# Standalone NMR data conversion service
SERVICE_LEVEL=${SERVICE_LEVEL}
SERVICE_DOMAIN=${SERVICE_DOMAIN}
SERVICE_HOST=${SERVICE_SUBDOMAIN}.${SERVICE_DOMAIN}
SERVICE_ADMIN_EMAIL=${SERVICE_ADMIN_EMAIL}
SERVICE_HELP_EMAIL=${SERVICE_HELP_EMAIL}

# Nginx
NGINX_LOG_FORMAT=${NGINX_LOG_FORMAT}

# Frontend (Flask)
FLASK_ENV=${SERVICE_LEVEL}

# PostgreSQL
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
DATABSE_URL=postgresql+psycopg2://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:5432/${POSTGRES_SERVICE_DB}
PREFECT_API_DATABASE_CONNECTION_URL=postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:5432/${POSTGRES_PREFECT_DB}

# GitHub Container Repository
MAXIT_CCD_SELF_RUNNER_TOKEN=${MAXIT_CCD_SELF_RUNNER_TOKEN}
UTILS_NMR_SELF_RUNNER_TOKEN=${UTILS_NMR_SELF_RUNNER_TOKEN}

EOF

echo Generated .env file.

source .env

( cd nginx; rm -f nginx.conf.template; ln -s nginx-${SERVICE_LEVEL}.conf.template nginx.conf.template )
sed -e 's/${SERVICE_HOST}/'"${SERVICE_HOST}"'/' < nginx/nginx.conf.template | \
sed -e 's/${NGINX_LOG_FORMAT}/'"${NGINX_LOG_FORMAT}"'/' > nginx/nginx.conf

echo Generated nginx/nginx.conf file.

sed -e 's/${SERVICE_HOST}/'"${SERVICE_HOST}"'/' < nginx/ssl.conf.template > nginx/ssl.conf

echo Generated nginx/ssl.conf file.

#
# Write init.sql
#

( cd postgres; rm -f init.sql.template; ln -s init-${SERVICE_LEVEL}.sql.template init.sql.template )
sed -e 's/${POSTGRES_USER}/'"${POSTGRES_USER}"'/' postgres/init.sql.template | \
sed -e 's/${POSTGRES_PASSWORD}/'"${POSTGRES_PASSWORD}"'/' | \
sed -e 's/${POSTGRES_SERVICE_DB}/'"${POSTGRES_SERVICE_DB}"'/g' | \
sed -e 's/${POSTGRES_PREFECT_DB}/'"${POSTGRES_PREFECT_DB}"'/g' > postgres/init.sql

echo Generated postgres/init.sql file.

#
# Write certbot.sh
#
sed -e 's/${SERVICE_HOST}/'"${SERVICE_HOST}"'/' certbot/certbot.sh.template | \
sed -e 's/${SERVICE_ADMIN_EMAIL}/'"${SERVICE_ADMIN_EMAIL}"'/g' > certbot/certbot.sh

echo Generated certbot/certbot.sh file.

#
# Frontend
#
if [[ ${SERVICE_DOMAIN} = "bmrb.io" ]] ; then

	( cd frontend/src
	  rm -f index.html site.config.ts
	  ln -s index.bmrb.html index.html
	  sed -e 's/${SERVICE_HELP_EMAIL}/'"${SERVICE_HELP_EMAIL}"'/g' bmrb.config.ts.template | \
	  sed -e 's/${SUCCESS_VALIDITY_PERIOD_IN_DAYS}/'"${SUCCESS_VALIDITY_PERIOD_IN_DAYS}"'/g' | \
	  sed -e 's/${FAILURE_VALIDITY_PERIOD_IN_DAYS}/'"${FAILURE_VALIDITY_PERIOD_IN_DAYS}"'/g' > bmrb.config.ts
	  ln -s bmrb.config.ts site.config.ts )

else

	( cd frontend/src
	  rm -f index.html site.config.ts
	  ln -s index.bmrbj.html index.html
	  sed -e 's/${SERVICE_HELP_EMAIL}/'"${SERVICE_HELP_EMAIL}"'/g' bmrbj.config.ts.template | \
	  sed -e 's/${SUCCESS_VALIDITY_PERIOD_IN_DAYS}/'"${SUCCESS_VALIDITY_PERIOD_IN_DAYS}"'/g' | \
	  sed -e 's/${FAILURE_VALIDITY_PERIOD_IN_DAYS}/'"${FAILURE_VALIDITY_PERIOD_IN_DAYS}"'/g' > bmrbj.config.ts
	  ln -s bmrbj.config.ts site.config.ts )

fi

echo Genearted frontend/src/index.html and frontend/src/site.config.ts files.
