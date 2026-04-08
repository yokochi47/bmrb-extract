#!/bin/bash

if [[ ! -e .env ]]; then

	./config.sh

	if [[ ! -e .env ]]; then
		echo "Error: Mssing .env file."
		exit 1
	fi

fi

source .env

if ! `command -v docker > /dev/null` ; then
	echo "Error: Docker is not installed."
	exit 1
fi

#
# Set hostname
#
if [[ -n "${SERVICE_HOST}" ]] && [[ $SERVICE_HOST != `hostname` ]] ; then
	sudo hostnamectl set-hostname $SERVICE_HOST
fi

#
# Create a directory for Docker volume (pg_data)
#
if [[ -n "${POSTGRES_DATA_VOL_DIR}" ]] && [[ ! -d "${POSTGRES_DATA_VOL_DIR}" ]] ; then

	sudo mkdir -p ${POSTGRES_DATA_VOL_DIR}
	sudo chown ${USER}:${USER} ${POSTGRES_DATA_VOL_DIR}

fi

#
# Create a Docker volume (pg_data)
#
docker volume inspect ${POSTGRES_DATA_VOL_LABEL} > /dev/null 2>&1 \
	|| docker volume create --driver local \
		--opt type=none \
		--opt device=${POSTGRES_DATA_VOL_DIR} \
		--opt o=bind \
		${POSTGRES_DATA_VOL_LABEL}

#
# Create a directory for Docker volume (nginx_logs)
#
if [[ -n "${NGINX_LOG_VOL_DIR}" ]] && [[ ! -d "${NGINX_LOG_VOL_DIR}" ]] ; then

	sudo mkdir -p ${NGINX_LOG_VOL_DIR}
	sudo chown ${USER}:${USER} ${NGINX_LOG_VOL_DIR}

fi

#
# Create a Docker volume (nginx_logs)
#
docker volume inspect ${NGINX_LOG_VOL_LABEL} > /dev/null 2>&1 \
	|| docker volume create --driver local \
		--opt type=none \
		--opt device=${NGINX_LOG_VOL_DIR} \
		--opt o=bind \
		${NGINX_LOG_VOL_LABEL}

#
# Setup GitHub Action Runner
#
if [[ -n "${ACTION_RUNNER_DIR}" ]] ; then

	mkdir -p ${ACTION_RUNNER_DIR} && \
		(
			cd ${ACTION_RUNNER_DIR}

			printf "\n${SERVICE_HOST}\n\n\n" > .default_self_runnder_config

			#
			# Download runner package
			#
			if [[ ! -e ${ACTION_RUNNER_TARBALL} ]] ; then

				curl -o ${ACTION_RUNNER_TARBALL} -L https://github.com/actions/runner/releases/download/v${ACTION_RUNNER_VER}/${ACTION_RUNNER_TARBALL}

				for repo in "${ACTION_RUNNER_REPOS[@]}" ; do

					rm -rf ${ACTION_RUNNER_DIR}/$repo

				done

			fi

			#
			# Validate the hash
			#
			echo
			echo "${ACTION_RUNNER_CHECK_SUM}  ${ACTION_RUNNER_TARBALL}" | shasum -a 256 -c || exit 1

			#
			# Extract GutHub Action Runner installer and configuration
			#
			for repo in "${ACTION_RUNNER_REPOS[@]}" ; do

				run_dir=../${ACTION_RUNNER_DIR}/$repo
				case $repo in
					${MAXIT_CCD_REPO})
						token=${MAXIT_CCD_SELF_RUNNER_TOKEN}
						;;
					${UTILS_NMR_REPO})
						token=${UTILS_NMR_SELF_RUNNER_TOKEN}
						;;
					*)
						echo "Error: ${repo} is unknown repository."
						exit 1
						;;
				esac

				mkdir -p $run_dir && \
					(
						cd $run_dir

						[[ ! -e ${ACTION_RUNNER_TARBALL} ]] && ln -s ../${ACTION_RUNNER_TARBALL} .

						[[ ! -e run.sh ]] && tar cvf ${ACTION_RUNNER_TARBALL}

						if [[ ! -e .runner ]] ; then
							./config.sh --url ${ACTION_RUNNER_BASE_REPO}/$rep --token $token < ../.default_self_runner_config && \
								sudo ./svc.sh install && sudo ./svc.sh start

						fi
					)

			done

		)

fi

./maxit_ccd_runner_svc.sh status

./utils_nmr_runner_svc.sh status

#
# Enable Docker Swarm for rolling update
#
docker system info | grep Swarm | grep active > /dev/null || docker swarm init

echo
echo "<<< Docker system info >>>"
echo

docker system info

#
# Pull Docker containers
#

for repo in "${ACTION_RUNNER_REPOS[@]}" ; do

	case $repo in
		${MAXIT_CCD_REPO})
			container_repo=${MAXIT_CCD_CONTAINER_REPO}
			;;
		${UTILS_NMR_REPO})
			container_repo=${UTILS_NMR_CONTAINER_REPO}
			;;
		*)
			echo "Error: ${repo} is unknown repository."
			exit 1
			;;
	esac

	[[ `docker images -q $container_repo | wc -l` = 0 ]] && docker pull $container_repo

done

#
# Setup Docker Swarm Cluster
#

for repo in "${ACTION_RUNNER_REPOS[@]}" ; do

	case $repo in
		${MAXIT_CCD_REPO})
			container_repo=${MAXIT_CCD_CONTAINER_REPO}
			;;
		${UTILS_NMR_REPO})
			container_repo=${UTILS_NMR_CONTAINER_REPO}
			;;
		*)
			echo "Error: ${repo} is unknown repository."
			exit 1
			;;
	esac

	docker service ps $repo > /dev/null || \
		(docker service create -q --replicas 3 --name $repo --update-delay 20s $container_repo &)

done

echo "<<< Docker service info >>>"
echo

docker service ls

echo
echo "<<< Docker volume info >>>"
echo

docker volume ls

COMPOSE_BAKE=true docker compose build --build-arg OPENSSL_VERSION=${OPENSSL_VERSION} --build-arg NGINX_VERSION=${NGINX_VERSION} --build-arg CACHEBUST=$(date +%s) # --no-cache

# Performance tuning
sudo sysctl -w net.core.rmem_max=7500000
sudo sysctl -w net.core.wmem_max=7500000

