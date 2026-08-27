# Merging upstream into `local-deploy`

This branch adds a self-contained local deployment (see `LOCAL_DEPLOYMENT.md`)
while leaving upstream's own deployment machinery in place and unmodified. It is
meant to be a long-lived fork of the *deployment*, not of the application, so
`git merge main` should stay routine.

This document is the thing to read when it isn't.

**Baseline:** `docker/upstream-baseline` records the upstream commit this branch
was last reconciled against. It started at `d09b0643`.

---

## The merge procedure

```bash
git checkout main && git pull                  # refresh the upstream mirror
git checkout local-deploy
git merge main                                 # resolve conflicts — see Part A
./docker/check-upstream-drift.sh               # catches what merging cannot — Part B
docker compose up -d --build                   # rebuild everything
git rev-parse main > docker/upstream-baseline  # record the reconciliation
git commit -am 'Merge upstream; reconcile local deployment'
```

The drift check is the important step. This branch edits almost nothing, so a
clean merge is *not* evidence that the local deployment still works: most of the
coupling is to the **shape** of upstream files this branch reads or shadows, and
git has nothing to say about those.

---

## Part A — files this branch edits

Conflicts here are loud and git will show them. Around 30 lines across five
files, each one routing an existing call through new code rather than rewriting
logic. Every one uses a `getattr(..., default)` fallback, so the patched code
still runs against an upstream-rendered `site_config.py` — if you ever need to
back a change out, deleting it is safe.

| File | Change | Why it must survive |
| --- | --- | --- |
| `backend/app/app.py` | `_send_email()` calls `core.local_mail.send_message()` | SMTP port/TLS/auth and `MAIL_BACKEND=log` |
| `backend/app/app.py` | `resume_url` uses `SERVICE_BASE_URL` | upstream hardcodes `https://<host>`, wrong on plain HTTP and non-default ports |
| `backend/app/app.py` | dropped `import smtplib`, dropped `SMTP_SERVER` from the `core.site_config` import | dead after the above; purely cosmetic |
| `backend/app/features/auth.py` | magic link uses `SERVICE_BASE_URL` | same |
| `backend/app/features/auth.py` | `_secure_cookie()` honours `AUTH_COOKIE_SECURE` | a `Secure` cookie is withheld on `http://`, so logins silently never stick |
| `prefect/flows/core/process_session.py` | `_send_admin_email()` → `core.local_mail` | as above |
| `prefect/flows/core/cleanup.py` | `_send_admin_email()` → `core.local_mail` | as above |
| `prefect/flows/core/exchange.py` | `_send_admin_email()` → `core.local_mail` | as above |
| `.gitignore` | added `data/` | keeps the runtime data tree out of git |

**If upstream rewrites one of the mail functions,** take upstream's version whole
and re-apply the two-line swap:

```python
from core.local_mail import send_message as _send_message
_send_message(msg, timeout=30)
```

**If upstream adds a *new* mail call site or a new user-facing link,** git will
not conflict — the drift check catches both.

---

## Part B — files this branch shadows, forks or reads

This branch does not edit these, so **upstream changes to them merge cleanly and
change nothing**, which is exactly the problem. `docker/check-upstream-drift.sh`
watches every one and tells you which changed since the baseline.

### Read at runtime — the shape matters

| Upstream file | Read by | What goes wrong |
| --- | --- | --- |
| `backend/app/core/site_config.py.template` | `docker/common/render-site-config.sh` (`envsubst`, at every container start) | a **new `${VAR}`** renders as an empty string unless it is added to the `x-site-env` anchor in `compose.yaml` and to `.env.local.template`. A **removed constant** breaks the appended override block (`MAXIT_MEMORY_LIMIT`, `PDF_REPORT_IMAGE`, `BMRB_ENTRY_DIR_URL` use the template's own value as their default) — that one is loud, the renderer executes the module. |
| `postgres/*-service.sql.template`, `init-prefect.sql.template` | `docker/postgres/initdb/10-bmrb-extract.sh` | a new `${VAR}` renders empty into the DDL. The postgres service only receives six variables. Also see **schema changes** below. |
| `postgres/init-service.sql.template` | the same script patches `AS ENUM ('bmrb.io', 'pdbj.org')` to add a custom `SERVICE_DOMAIN` | guarded — the script aborts if that literal is gone. Only fires when `SERVICE_DOMAIN` is neither upstream site. |
| `prefect/flows/shared/core` (symlink) | `docker/prefect/Dockerfile` | the `/flows` + `/backend/app` layout in the image exists only so `../../../backend/app/core` resolves. Retarget the symlink and imports break loudly. |
| `frontend/angular.json` | `docker/nginx/Dockerfile` copies `dist/bmrb_extract/browser` | a changed output path fails the build. Loud. |
| `backend/requirements.txt` | `docker/backend/Dockerfile` | picked up automatically. Nothing to do. |

### Forked — upstream improvements do not flow in

| Upstream file | Local counterpart | What to carry over by hand |
| --- | --- | --- |
| `compose.yml` | `compose.yaml` | a **new service**, an image tag bump (`postgres:18-alpine`, `redis:7-alpine`, `prefecthq/prefect:3-latest`), a changed healthcheck, a new mount |
| `.env.template` | `.env.local.template` | new settings the application actually reads. Ignore the Action-runner, Swarm, certbot and nginx-source variables |
| `nginx/nginx-production.conf.template` | `docker/nginx/bmrb-extract.conf.template` | a new `location`, a new proxy header, a changed body-size or timeout. Deliberately absent here: the `/.well-known/acme-challenge/` block, the HTTPS/QUIC server, file logging + logrotate |
| `frontend/src/bmrb.config.ts.template` | `docker/frontend/site.config.local.ts` | a new export. Loud (the build fails), but adding it to the TS is only half — it also needs a `--arg` in the substitution payload, an entry in the nginx service environment, and a line in `.env.local.template`, or it silently keeps its fallback |
| `frontend/src/index.bmrb.html` | `docker/frontend/index.local.html` | a new `<meta>`, font, or CSP tag. Currently identical apart from the `@@BMRBX_@@` placeholders |
| `pdf/Dockerfile` | `docker/pdf/Dockerfile` | a new apt/pip dependency, a version bump, a newly copied file. The fork exists only to build from the repo root instead of needing `pdf/build.sh` to stage sources first |
| `pdf/build.sh` | inlined into `docker/pdf/Dockerfile` | if it starts staging something new, add a `COPY` |
| `nginx/share/{401,403}.html` | copied by `docker/nginx/Dockerfile` | a rename fails the build. Loud |
| `config.sh`, `setup.sh`, `start.sh`, `stop.sh`, `reload_wfe.sh`, `reset_db.sh`, `*_runner_svc.sh`, `certbot/` | none — unused | nothing, unless `setup.sh` grows a step that is genuinely required rather than host provisioning |

### Not covered by the drift check

**A new third-party import in a flow.** `docker/prefect/Dockerfile` installs a
fixed list; a new dependency fails at *flow run* time, not at build time. After
a merge that touches `prefect/flows/`:

```bash
grep -rhoE '^(import|from) [a-z_][a-zA-Z0-9_.]*' prefect/flows/core/*.py \
  | awk '{print $2}' | cut -d. -f1 | sort -u
```

Anything outside `asyncio core datetime email hashlib json os pathlib prefect re
shlex shutil sqlalchemy subprocess sys traceback workspace` needs adding to the
`pip install` line.

**A schema change in `postgres/init-service.sql.template`.** The init SQL runs
only when the data directory is empty, so an existing local database will not
pick it up — the same constraint upstream has. Either apply the DDL by hand:

```bash
# POSTGRES_USER is set in .env, not in your shell — pass it explicitly
docker compose exec postgres psql -U bmrb_extract -d internal
```

or, if the data is expendable, start clean:

```bash
docker compose down && rm -rf data/postgres && docker compose up -d --build
```

Note that `postgres/reset.sql` is generated by `config.sh`, which this branch
never runs, so it may be stale or absent.

---

## Part C — what "the local behavior" is

These are the invariants worth protecting. If a merge breaks one, the deployment
is no longer doing what this branch exists to do.

1. **One compose file, no host-side setup.** `cp .env.local.template .env` then
   `docker compose up -d --build` is the entire procedure. Nothing renders a file
   onto the host; nothing needs `sudo`; no Swarm, no Action runners.
2. **Every setting is re-read at container start.** Editing `.env` and running
   `docker compose up -d` applies it. The sole exception is the database schema,
   which PostgreSQL initialises once.
3. **The images are self-contained.** No service depends on an image a previous
   command happened to leave behind, and application source is baked in, not
   bind-mounted.
4. **Persistent data lives in `BMRBX_DATA_DIR`** (default `./data`) as bind
   mounts — postgres, redis, prefect, archive, workspace — not in Docker volumes.
5. **Plain HTTP works end to end**, including login: `AUTH_COOKIE_SECURE=false`
   and emailed links built from `SERVICE_BASE_URL`.
6. **Mail is configurable**, including authenticated submission and the
   no-mail-server `MAIL_BACKEND=log` path.

---

## Part D — inventory of what this branch adds

Nothing here exists upstream, so none of it can conflict.

```
compose.yaml                          the whole stack
.env.local.template                   every setting, documented
.dockerignore                         keeps host-rendered artefacts out of the images
LOCAL_DEPLOYMENT.md                   how to run it
MERGING_UPSTREAM.md                   this file
docker/check-upstream-drift.sh        run after every merge
docker/upstream-baseline              last reconciled upstream commit
docker/common/render-site-config.sh   renders core/site_config.py from the environment
docker/backend/{Dockerfile,entrypoint.sh}
docker/prefect/{Dockerfile,entrypoint.sh}
docker/postgres/{Dockerfile,initdb/10-bmrb-extract.sh}
docker/nginx/{Dockerfile,bmrb-extract.conf.template,entrypoint.d/*.sh}
docker/pdf/Dockerfile
docker/frontend/{site.config.local.ts,index.local.html}
backend/app/core/local_mail.py        the one new file inside an upstream directory
```

`backend/app/core/local_mail.py` sits in an upstream directory but is a new file,
so it merges cleanly. It is reachable from the flows too, through the
`prefect/flows/shared/core` symlink.
