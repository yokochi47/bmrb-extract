# Local deployment

This branch (`local-deploy`) adds a self-contained way to run bmrb-extract on
your own machine: **one compose file, one `.env`, no host-side setup scripts.**

```bash
cp .env.local.template .env
$EDITOR .env                    # at minimum: AUTH_SECRET, POSTGRES_PASSWORD, the addresses
docker compose up -d --build
```

Then open <http://localhost:8080>.

Everything else — building the frontend, rendering the nginx config, creating
the databases, registering the Prefect deployments, building the PDF-report
image — happens inside the containers.

## What it changes

Upstream deploys through `./config.sh` → `./setup.sh` → `./start.sh`, which
render seven files onto the host, install two GitHub Action runners as systemd
services, initialise Docker Swarm, and register the Prefect deployments from
outside the stack. None of that runs here.

| Upstream | Here |
| --- | --- |
| `config.sh` renders `.env`, `nginx.conf`, `ssl.conf`, `postgres/init.sql`, `certbot.sh`, `site.config.ts`, `site_config.py` on the host | Every one of those is produced by a container entrypoint from the environment, on each start |
| `setup.sh` does `hostnamectl`, `sudo mkdir`, `docker volume create --opt o=bind`, GitHub Action runners, `docker swarm init`, two `docker service create` | Deleted. The conversion images are pulled on first use by the `docker run` calls in the flows |
| `setup.sh` calls `./pdf/build.sh`, which stages files into `pdf/` before building | The `pdf-report` compose service builds from the repo root and copies the canonical sources directly |
| `nginx/Dockerfile` does `COPY --from=bmrb-extract-frontend:latest`, so the build depends on an image an earlier command left behind | One multi-stage build compiles the Angular app and serves it; there is no `frontend` service |
| `start.sh` runs `prefect deploy --all` from the host after the stack is up | The worker entrypoint registers the deployments itself |
| Angular config compiled into the bundle — changing the help address means a rebuild | Substituted into the built bundle on every start |
| nginx compiled from source with QUIC, needing host `sysctl` tuning | Official `nginx:alpine`, plain HTTP |
| Let's Encrypt via a `certbot` container | No TLS here — front it with your own reverse proxy |
| Postgres data in a Docker volume backed by `/var/lib/pg_data_<level>` | A bind mount under `BMRBX_DATA_DIR` |
| SMTP hardcoded to port 25, no auth, no TLS | `SMTP_SERVER`/`PORT`/`USER`/`PASSWORD`/`STARTTLS`/`SSL`, plus `MAIL_BACKEND=log` |

The upstream files are all still there and untouched, so `git pull` /
`git merge` upstream stays clean. The new compose file is named `compose.yaml`,
which Docker Compose prefers over the upstream `compose.yml`, so plain
`docker compose` commands pick it up with no `-f` flag.

**Merging upstream in future: see [MERGING_UPSTREAM.md](MERGING_UPSTREAM.md).**
Most of what this branch depends on is the *shape* of upstream files it does not
edit, so a clean merge does not prove the deployment still works. Run
`./docker/check-upstream-drift.sh` after every merge.

## Layout

```
compose.yaml                          the whole stack
.env.local.template                   every setting, documented — copy to .env
.dockerignore                         keeps host-rendered artefacts out of the images
.gitattributes                        pins README.md with merge=ours (see MERGING_UPSTREAM.md)
docker/
  common/render-site-config.sh        renders core/site_config.py from the environment
  backend/{Dockerfile,entrypoint.sh}
  prefect/{Dockerfile,entrypoint.sh}  bakes in the flows; registers the deployments
  postgres/Dockerfile                 renders + applies the init SQL on first start
  postgres/initdb/10-bmrb-extract.sh
  nginx/Dockerfile                    builds the frontend AND serves it
  nginx/bmrb-extract.conf.template
  nginx/entrypoint.d/*.sh             injects config into the bundle, renders the server block
  pdf/Dockerfile                      root-context build of the report generator
  frontend/site.config.local.ts       runtime-resolved replacement for the generated site config
  frontend/index.local.html
backend/app/core/local_mail.py        SMTP with host/port/TLS/auth, or MAIL_BACKEND=log
```

## Everyday commands

```bash
docker compose up -d --build     # start (or apply an .env change that needs a rebuild)
docker compose up -d             # apply an .env change — no rebuild needed
docker compose down              # stop
docker compose logs -f backend   # backend log (and magic-link URLs under MAIL_BACKEND=log)
docker compose logs -f prefect-worker
docker compose ps
```

Rebuild only after changing source: `docker compose build nginx` for the
frontend, `backend` for the API, `prefect-worker` for the flows.

## Reaching the internals

The stack runs on its own private Docker network, `bmrb-extract_internal`.
Containers find each other by service name — `postgres:5432`, `redis:6379`,
`backend:8000`, `prefect-server:4200` — and none of that involves the host.

**Host ports are the only thing this stack shares with the rest of the machine**,
so they are the only place a collision can happen. It claims exactly two:

| Port | Service | Set by |
| --- | --- | --- |
| `${HTTP_BIND}:${HTTP_PORT}` → 80 | nginx — the service itself | `HTTP_BIND`, `HTTP_PORT` |
| `127.0.0.1:14200` → 4200 | Prefect UI | `PREFECT_PUBLISH` |

PostgreSQL and Redis are deliberately not published. Nothing outside the stack
needs them, and binding 5432 collides with any other PostgreSQL on the machine.

For a database prompt, go in through the container rather than over the network:

```bash
docker compose exec postgres psql -U bmrb_extract -d internal
docker compose exec redis redis-cli
```

For a GUI client that has to speak TCP, forward the port ad hoc without changing
the stack — attach a throwaway container to the same network and let it publish:

```bash
docker run --rm -it --network bmrb-extract_internal \
  -p 127.0.0.1:15432:5432 alpine/socat \
  tcp-listen:5432,fork,reuseaddr tcp-connect:postgres:5432
```

Then point the client at `127.0.0.1:15432`. Ctrl-C to remove it again.

If you run several Compose projects on this host, give each a distinct
`COMPOSE_PROJECT_NAME` — container names, the network and the default volume
names are all prefixed with it, so nothing else collides.

## How runtime configuration works

**Backend and flows.** `docker/common/render-site-config.sh` runs `envsubst`
over the *upstream* `backend/app/core/site_config.py.template` — the same
substitution `config.sh` does, but in the container at start — then appends a
block of local-only settings that read `os.environ` at import time. Using the
upstream template means anything upstream adds to it is picked up for free.

It refuses to start on an unset required variable, on a relative
`ARCHIVE_VOL_DIR`/`WORKSPACE_VOL_DIR`, or on a value that still contains a
literal `${...}`, and it executes the rendered module so the template's own
assertions fail here rather than mid-request.

**Frontend.** The Angular bundle is compiled once at image build time from
`docker/frontend/site.config.local.ts`, which resolves its values from a single
base64 placeholder. The nginx entrypoint copies the pristine build into the
served directory and substitutes the current environment into it on every start.
Base64 keeps the payload free of any character that could terminate the
JavaScript string literal it lands in, whichever quote style the minifier chose.
Every value has a fallback, so an unsubstituted bundle still boots.

## Things worth knowing

**The Docker socket is mounted into the worker.** The conversion tasks run
maxit-ccd, py-wwpdb_utils_nmr and the PDF generator as sibling containers on the
host daemon — that is how upstream works and changing it would mean rewriting
the flows. It gives the worker container root-equivalent control of the host.
Run this on a machine where that is acceptable.

**Archive and workspace paths must be absolute.** The worker passes them
straight to `docker run -v`, which the daemon resolves against the *host*. A
relative path there would silently become a named volume. `BMRBX_DATA_DIR`
defaults to `${PWD}/data`, resolved by Compose from the directory you run it in;
make it a literal path if you invoke compose from somewhere else.

**Schema settings are first-start-only.** PostgreSQL runs the init SQL only when
its data directory is empty, so `SERVICE_DOMAIN` and the `POSTGRES_*_DB` names
take effect once. Changing them later means starting from an empty
`${BMRBX_DATA_DIR}/postgres`. Everything else in `.env` is re-read on restart.

**`session.processing_site` is an enum** of the two upstream sites. A
`SERVICE_DOMAIN` outside that set is added to the enum automatically at init.

**Plain HTTP means `AUTH_COOKIE_SECURE=false`.** Browsers withhold a `Secure`
cookie from an `http://` origin, so leaving it true means logins never stick.
If you put HTTPS in front of this, set it true and update `SERVICE_BASE_URL`.

**`SERVICE_BASE_URL` is what goes in emails.** Upstream hardcodes
`https://<SERVICE_HOST>`, which is wrong for a plain-HTTP instance on a
non-default port. It must be reachable from wherever people read their mail.

**No mail server?** Set `MAIL_BACKEND=log` and read login links out of
`docker compose logs -f backend`.

**The upload page calls `https://api.bmrb.io/v2` from the browser** for BMRB-ID
validation. That is not proxied through `/api/`, so it needs outbound internet
from the *client*, and it fails closed if that host is unreachable.

**Redis logs a memory-overcommit warning.** Upstream's `setup.sh` sets
`vm.overcommit_memory=1` on the host. That is not namespaced, so a container
cannot do it. Run `sudo sysctl -w vm.overcommit_memory=1` if you want it gone;
the warning is harmless for this workload.

## Changes to upstream source

Five files, ~30 lines, all of it routing through new code rather than rewriting
existing logic — kept small so upstream merges stay clean:

- `backend/app/core/local_mail.py` (**new**) — SMTP with host/port/TLS/auth,
  plus the `log` backend.
- `backend/app/app.py`, `prefect/flows/core/{process_session,cleanup,exchange}.py` —
  the four `smtplib.SMTP(SMTP_SERVER, 25)` call sites now go through it.
- `backend/app/app.py`, `backend/app/features/auth.py` — the two hardcoded
  `https://{SERVICE_HOST}` links use `SERVICE_BASE_URL`.
- `backend/app/features/auth.py` — `_secure_cookie()` honours
  `AUTH_COOKIE_SECURE`.

Each reads its setting with a `getattr(..., default)` fallback, so the code still
works unmodified against an upstream-rendered `site_config.py`.
