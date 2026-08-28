#!/bin/bash

set -eu

if [[ ! -e .env ]] ; then

  ./config.sh

  if [[ ! -e .env ]] ; then
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
# Create a directory for archive storage
#
if [[ -n "${ARCHIVE_VOL_DIR}" ]] && [[ ! -d "${ARCHIVE_VOL_DIR}" ]] ; then

  sudo mkdir -p ${ARCHIVE_VOL_DIR}
  sudo chown ${USER}:${USER} ${ARCHIVE_VOL_DIR}

fi

#
# Create a directory for the conversion workspace
#
if [[ -n "${WORKSPACE_VOL_DIR}" ]] && [[ ! -d "${WORKSPACE_VOL_DIR}" ]] ; then

  sudo mkdir -p ${WORKSPACE_VOL_DIR}
  sudo chown ${USER}:${USER} ${WORKSPACE_VOL_DIR}

fi

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

      printf "\n${SERVICE_HOST}\n\n\n" > .default_self_runner_config

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

            [[ ! -e run.sh ]] && tar xzf ${ACTION_RUNNER_TARBALL}

            if [[ ! -e .runner ]] ; then
              ./config.sh --url ${ACTION_RUNNER_BASE_REPO}/$repo --token $token < ../.default_self_runner_config && \
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
docker system info | grep Swarm | grep active > /dev/null 2>&1 || docker swarm init

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
      container_image=${MAXIT_CCD_IMAGE}
      ;;
    ${UTILS_NMR_REPO})
      container_image=${UTILS_NMR_IMAGE}
      ;;
    *)
      echo "Error: ${repo} is unknown repository."
      exit 1
      ;;
  esac

  [[ `docker images -q $container_image | wc -l` = 0 ]] && docker pull $container_image

done

#
# Setup Docker Swarm Cluster
#
for repo in "${ACTION_RUNNER_REPOS[@]}" ; do

  case $repo in
    ${MAXIT_CCD_REPO})
      container_image=${MAXIT_CCD_IMAGE}
      ;;
    ${UTILS_NMR_REPO})
      container_image=${UTILS_NMR_IMAGE}
      ;;
    *)
      echo "Error: ${repo} is unknown repository."
      exit 1
      ;;
  esac

  docker service ps $repo > /dev/null 2>&1 || \
    (docker service create -q --replicas 3 --name $repo --update-delay 20s \
      --mount type=bind,source=${ARCHIVE_VOL_DIR},target=${ARCHIVE_BASE_PATH} \
      --mount type=bind,source=${WORKSPACE_VOL_DIR},target=${WORKSPACE_BASE_PATH} \
      $container_image &)

done

echo "<<< Docker service info >>>"
echo

docker service ls

echo
echo "<<< Docker volume info >>>"
echo

docker volume ls

BUILD_OPTION=
if [[ $# -ge 1 ]] ; then
 BUILD_OPTION=$1
fi

# nginx depends on frontend
COMPOSE_BAKE=true docker compose build frontend

COMPOSE_BAKE=true docker compose build --build-arg NGINX_VERSION=${NGINX_VERSION} --build-arg CACHEBUST=$(date +%s) ${BUILD_OPTION}

# PDF-report generator image (bmrb-extract-pdf-report:local). Not a compose
# service — built standalone here so a clean install has it; the deferred
# convert_pdf Prefect task runs it via `docker run`.
./pdf/build.sh

# Tweak for HTTP/3 (UDP)
net_core_mem_max=7500000

if [[ "`sysctl -n net.core.rmem_max`" -lt $net_core_mem_max ]] ; then
  sudo sysctl -w net.core.rmem_max=$net_core_mem_max
fi

if [[ "`sysctl -n net.core.wmem_max`" -lt $net_core_mem_max ]] ; then
  sudo sysctl -w net.core.wmem_max=$net_core_mem_max
fi

# Tweak for Redis
if [[ "`sysctl -n vm.overcommit_memory`" -eq 0 ]] ; then
  sudo sysctl -w vm.overcommit_memory=1
fi

