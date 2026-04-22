#!/bin/bash

if [[ ! -e .env ]] ; then
  echo "Error: Run config.sh at first."
  exit 1
fi

source .env

repo_dir=$ACTION_RUNNER_DIR/$UTILS_NMR_REPO

if [[ $# -gt 1 ]] ; then
  echo
  echo "<<< GitHub Action Runner: $repo_dir >>>"
fi

if [[ ! -d $repo_dir ]] ; then
  echo "Error: Mssing $repo_dir directory."
  exit 1
fi

( cd $repo_dir ; sudo ./svc.sh $1 )

