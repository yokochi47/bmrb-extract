<p align="center">
  <a href="https://bmrb-extract.pdbj.org"><img src="frontend/public/bmrb_extract_logo.svg" width="300" height="140" alt="bmrb_extract"/></a>
</p>

**bmrb_extract** is an NMR data conversion service that simplifies data deposition to PDB (via OneDep) and BMRB (via BMRBdep) by creating a single NMR data file from various software-native formats. The service is designed to be hosted by [BMRB](https://bmrb.io) and [BMRBj](https://bmrbj.pdbj.org).

> **This is the `local-deploy` branch.** It tracks
> [yokochi47/bmrb-extract](https://github.com/yokochi47/bmrb-extract) and adds a
> self-contained way to run the service on a single machine. The application is
> upstream's; only the deployment differs. Upstream's own README is still there —
> `git show main:README.md`.

## Requirements

- Docker Engine, with the Compose plugin

That is all — the Node and Angular toolchain, the Python environment and the
PDF-report stack are built inside the images.

## Install

```shell
cp .env.local.template .env
$EDITOR .env          # at minimum: AUTH_SECRET, POSTGRES_PASSWORD, the addresses
docker compose up -d --build
```

Then open <http://localhost:8080>.

Building the frontend, rendering the nginx config, creating the databases,
registering the Prefect deployments and building the PDF-report image all happen
inside the containers. There is no host-side setup step and nothing needs `sudo`.

Every setting lives in `.env` and is re-read at container start, so changing one
and running `docker compose up -d` applies it.

```shell
docker compose up -d --build     # start, or apply a change needing a rebuild
docker compose up -d             # apply an .env change
docker compose down              # stop
docker compose logs -f backend   # backend log
```

**→ [LOCAL_DEPLOYMENT.md](LOCAL_DEPLOYMENT.md)** — every setting, how runtime
configuration works, and the things worth knowing before running it.

## Tracking upstream

**→ [MERGING_UPSTREAM.md](MERGING_UPSTREAM.md).** This branch edits barely any
upstream source, so a clean `git merge main` is *not* evidence the deployment
still works — most of the coupling is to the shape of files it does not edit.
After every merge:

```shell
./docker/check-upstream-drift.sh
```

One-time setup in each clone, so this README survives upstream's edits to theirs
without conflicting (see `.gitattributes`):

```shell
git config merge.ours.driver true
```

## Upstream deployment

`config.sh`, `setup.sh`, `start.sh`, `reload_wfe.sh` and `reset_db.sh` are
upstream's production deployment for the BMRB and BMRBj sites. They are
untouched and still work; this branch simply does not use them.
