# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

**bmrb_extract** is an NMR data conversion service that converts software-native NMR formats into a single file for deposition to PDB (via OneDep) and BMRB (via BMRBdep). It is deployed at two production sites: `bmrb-extract.bmrb.io` (UConn, US/Eastern) and `bmrb-extract.pdbj.org` (Osaka, Asia/Tokyo).

## Commands

### Service lifecycle (Docker-based)
```bash
./config.sh       # interactive setup — generates .env, nginx.conf, init.sql, site_config.py, etc.
./setup.sh        # build Docker images, create volumes, init GitHub Action runners, start Docker Swarm services
./start.sh        # docker compose up -d (all services)
./stop.sh         # docker compose down
./reload_wfe.sh   # rebuild frontend + nginx only (for frontend development)
```

### Frontend (Angular 21)
```bash
cd frontend
npm start         # dev server
npm run build     # production build
npm run lint      # ESLint
npm test          # Vitest
```

### Backend (Flask)
The backend runs inside Docker. There are no standalone run/test commands — use `docker compose logs backend` to inspect output.

## Architecture

### Docker Compose Stack
- **nginx** — custom-built (version pinned at `./config.sh` time from latest OpenSSL/Nginx releases), reverse proxy + HTTPS termination. Serves frontend static files from the `frontend` container via a shared volume.
- **frontend** — Angular build container. Only runs during `start.sh`/`reload_wfe.sh`; stops after nginx copies its `/data`. Restart manually with `reload_wfe.sh`.
- **backend** — Flask + Gunicorn on port 8000. Source is volume-mounted at `./backend/app:/app` so edits take effect without rebuild.
- **postgres** — PostgreSQL 18 (requires v18 for the built-in `uuidv7()` function). Two databases: `internal` (service data) and `prefect` (Prefect server).
- **redis** — Used by Prefect for messaging/cache broker.
- **prefect-server / prefect-services / prefect-worker** — Prefect 3 workflow orchestration. Worker uses `local-pool` (process type). Flows live in `./prefect/flows/`.

Additionally, Docker Swarm runs two external NMR processing services as replicated services (3 replicas each, rolling updates), updated via self-hosted GitHub Action runners:
- `maxit-ccd` (`ghcr.io/yokochi47/maxit-ccd:main`) — coordinate file conversion
- `py-wwpdb_utils_nmr` (`ghcr.io/yokochi47/py-wwpdb_utils_nmr:main`) — NMR data conversion

### Template / Configuration System
`config.sh` is the single source of truth for configuration. It reads `.env.template`, prompts for site-specific values, and uses `envsubst` to generate:
- `.env` — environment variables for all Docker services
- `nginx/nginx.conf` — from `nginx/nginx-{production,development}.conf.template`
- `nginx/ssl.conf`
- `postgres/init.sql` — assembled from `setup-{level}.sql.template` + `init-service.sql.template` + `init-prefect.sql.template`
- `certbot/certbot.sh`
- `frontend/src/index.html` and `frontend/src/site.config.ts` — symlinked to site-specific variants
- `backend/app/core/site_config.py`

**Never edit generated files directly.** Edit their `.template` counterpart and re-run `./config.sh`.

### Backend (`backend/app/`)
- `app.py` — Flask app entry point; async SQLAlchemy sessions via `asyncpg`
- `core/site_config.py` — generated site constants (service level, domain, DB URL, validity periods, etc.)
- `core/models.py` — **generated** SQLAlchemy ORM models. Source of truth is `postgres/init-service.sql.template`; regenerate with [omymodels](https://archon-omymodels-online.hf.space) when the schema changes.
- `features/` — feature modules (currently empty; business logic goes here)

### Database Schema (`postgres/init-service.sql.template`)
Key tables:
- `session` — one row per user UI session; tracks lifecycle from `created` → `uploading` → `processing` → `completed`/`failed`/`expired`. The `conversion_id` (INT, range-partitioned by site) is assigned when processing begins and forms the public ID (displayed as `C_<conversion_id>`).
- `upload_file` — files uploaded by the user, keyed by `(token, ordinal)`. Files **accumulate** across runs and are never physically deleted once a run has used them; `selected` controls participation and `run_number` records the draft run a file was uploaded for.
- `output_file` — converted output files, keyed by `(conversion_id, run_number, ordinal)`.
- `workflow` — Prefect task tracking per conversion+run, keyed by `(conversion_id, run_number, ordinal)` (`WfTaskCode` enum).
- `notification` / `communication` — email delivery records.

A session supports multiple processing runs: `session.latest_run_number` counts committed runs (0 before the first Process; the in-progress draft is always `latest_run_number + 1`), and each `POST /api/process` creates a git commit tagged `run-<N>` plus `output_file`/`workflow` rows carrying that `run_number`.

Conversion ID ranges: bmrb.io → 1000001–2000000, pdbj.org → 2000001–3000000, development → 8000001–9000000.

### Frontend (`frontend/src/`)
Angular 21 with standalone components and lazy-loaded routes. UI library: PrimeNG 21 + Tailwind CSS v4 + `tailwindcss-primeui`.

Routes: `info`, `preface`, `terms`, `privacy` (public pages). The `consent.to` component handles user consent before a session begins.

Site-specific content (branding, URLs, help email) is injected via the symlinked `site.config.ts` (generated from `bmrb.config.ts.template` or `bmrbj.config.ts.template`). Export `API_URL = '/api/'` is the browser-facing Flask API base path (proxied by nginx); do not use the Docker-internal `FLASK_API_URL` env var in Angular code.

### Session token flow
After the user consents on `/info`, `PageService.newConsent()` calls `POST /api/new_consent`, receives a UUIDv7 token, and navigates to `/info?token=<uuid>`. All subsequent navigation uses `queryParamsHandling: 'preserve'` in menu items and router links so the token stays in the URL. On page refresh or sharing a URL, `PageService` reads the token back from `window.location.search` and restores session state automatically.

Protected routes (upload, summary, download) must include `canActivate: [tokenGuard]` — defined in `app.routes.ts` — which redirects to `/info` when the token is absent. The guard is defined but not yet applied to any route since those pages are not yet implemented.
