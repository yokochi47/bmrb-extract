# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

**bmrb_extract** is an NMR data conversion service that converts software-native NMR formats into a single file for deposition to PDB (via OneDep) and BMRB (via BMRBdep). It is deployed at two production sites: `bmrb-extract.bmrb.io` (UConn, US/Eastern) and `bmrb-extract.pdbj.org` (Osaka, Asia/Tokyo).

The full user-facing flow — consent → upload → process → summary/validation → approve → download, plus passwordless login, "my sessions" and a help desk — is implemented. The one piece written but **not yet enabled in production** is cross-site data exchange (see *Prefect flows* below): the code is complete but no-ops until `PEER_HOST` is configured.

## Commands

### Service lifecycle (Docker-based)
```bash
./config.sh       # interactive setup — generates .env, nginx.conf, init.sql, site_config.py, etc.
./setup.sh        # build Docker images (incl. the PDF-report image via ./pdf/build.sh), create volumes, init GitHub Action runners, start Docker Swarm services
./start.sh        # docker compose up -d (all services)
./stop.sh         # docker compose down
./reload_wfe.sh   # rebuild frontend + nginx only (for frontend development)
./pdf/build.sh    # (re)build the PDF-report generator image (bmrb-extract-pdf-report:local); run after editing pdf/ — setup.sh runs it on a clean install
./reset_db.sh     # drop and re-initialize the databases
./install_systemd.sh  # one-time: install + enable bmrb-extract.service (runs setup.sh && start.sh at boot, stop.sh at shutdown)
```

### Frontend (Angular 21)
```bash
cd frontend
npm start         # dev server (ng serve, development configuration)
npm run build     # production build
npm run lint      # ESLint (angular-eslint + template accessibility)
npm test          # Vitest — configured, but there are no *.spec.ts files yet
```

The dev server talks to the browser-facing `API_URL = '/api/'`, which nothing serves outside the
Docker stack — run `./start.sh` alongside it, or use `./reload_wfe.sh` to iterate against the real
nginx. Per-site values come from the `site.config.ts` symlink, not from `src/environments/`
(Angular's `fileReplacements` mechanism is deliberately unused here).

### Backend (Flask)
The backend runs inside Docker. There are no standalone run/test commands — use `docker compose logs backend` to inspect output. `./backend/app` is bind-mounted into the container, so edits take effect on the next request without a rebuild.

### Prefect flows
```bash
docker compose exec prefect-worker sh -c "cd /flows && prefect deploy --all"   # redeploy after editing a flow
```

## Architecture

### Docker Compose Stack
- **nginx** — custom-built (version pinned at `./config.sh` time from latest OpenSSL/Nginx releases), reverse proxy + HTTPS termination. Serves frontend static files from the `frontend` container via a shared volume.
- **frontend** — Angular build container. Only runs during `start.sh`/`reload_wfe.sh`; stops after nginx copies its `/data`. Restart manually with `reload_wfe.sh`.
- **backend** — Flask + Gunicorn on port 8000. Source is volume-mounted at `./backend/app:/app`, and both storage trees (`ARCHIVE_VOL_DIR`, `WORKSPACE_VOL_DIR`) are bind-mounted.
- **postgres** — PostgreSQL 18 (requires v18 for the built-in `uuidv7()` function). Two databases: `internal` (service data) and `prefect` (Prefect server). `PGDATA` is overridden to a subdirectory of the volume, otherwise the data (and the conversion_id sequence) would be lost on container recreate.
- **redis** — Prefect messaging/cache broker **and** the auth rate-limit store (`AUTH_REDIS_DB`, default db 1; rate limiting fails open if Redis is down).
- **prefect-server / prefect-services / prefect-worker** — Prefect 3 workflow orchestration. Worker uses `local-pool` (process type). It mounts the host Docker socket (flows shell out to `docker run` for the conversion images), `./secrets` (peer SSH key for cross-site exchange), and `./backend/app:/backend/app:ro`.
- **certbot** — Let's Encrypt renewal loop sharing `./certbot/cert` and `./certbot/www` with nginx.

Additionally, Docker Swarm runs two external NMR processing services as replicated services (3 replicas each, rolling updates), updated via self-hosted GitHub Action runners:
- `maxit-ccd` (`ghcr.io/yokochi47/maxit-ccd:main`) — coordinate file conversion
- `py-wwpdb_utils_nmr` (`ghcr.io/yokochi47/py-wwpdb_utils_nmr:main`) — NMR data conversion

### Storage layout
Two bind-mounted trees, kept deliberately separate so a conversion (which may edit its inputs in place) never contaminates the uploads:

- **Archive** — `<ARCHIVE_BASE_PATH>/<token>/` — the git-managed upload archive. `POST /api/upload` writes here; `POST /api/process` commits and tags `run-<N>`.
- **Workspace** — `<WORKSPACE_BASE_PATH>/<conversion_id>/` — `cache/` (NmrDpUtility cache, shared across all runs of the conversion) plus `<run_number>/{input,output,work,log}/`. Selected uploads are copied from the archive into `input/` before any conversion runs; `work/` is deleted when the flow finishes.

`prefect/flows/core/workspace.py` defines this scheme, and `backend/app/app.py` mirrors it. **Keep the two in sync.**

### Template / Configuration System
`config.sh` is the single source of truth for configuration. It reads `.env.template`, prompts for site-specific values, and uses `envsubst` to generate:
- `.env` — environment variables for all Docker services
- `nginx/nginx.conf` — from `nginx/nginx-{production,development}.conf.template`
- `nginx/ssl.conf`
- `postgres/init.sql` — assembled from `setup-service.sql.template` + `init-service.sql.template` + `init-prefect.sql.template`
- `certbot/certbot.sh`
- `frontend/src/index.html` and `frontend/src/site.config.ts` — symlinked to site-specific variants
- `backend/app/core/site_config.py`
- `systemd/bmrb-extract.service` and `systemd/bmrb-extract.sudoers` — the boot/shutdown unit and its scoped sudoers fragment, installed to `/etc` by `./install_systemd.sh` (`systemd/99-bmrb-extract.sysctl.conf` is static, not generated)

**Never edit generated files directly.** Edit their `.template` counterpart and re-run `./config.sh`.

**Schema changes on live sites.** There is no migration mechanism: `postgres/init.sql` is mounted at `/docker-entrypoint-initdb.d/` and so runs **only on a fresh volume**. After changing `postgres/init-service.sql.template`, re-run `./config.sh` (which covers fresh installs) and apply the equivalent idempotent DDL by hand at each production site:

```bash
source .env
docker exec -i bmrb-extract-postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_SERVICE_DB" \
  -c 'ALTER TABLE <table> ADD COLUMN IF NOT EXISTS <col> <type>;'
```

Keep `postgres/reset-service.sql.template` in step too — every table must appear in its `DROP TABLE` list, or `CREATE TABLE IF NOT EXISTS` will silently skip the change and even `./reset_db.sh` will not reproduce a fresh install.

### Backend (`backend/app/`)
- `app.py` — Flask app entry point (async SQLAlchemy via `asyncpg`, `NullPool` because Flask async creates an event loop per request) and the whole main API. Roughly half the file is report-parsing and chart-building helpers feeding the analysis endpoints. Endpoints, grouped:
  - *session lifecycle* — `POST /api/new_consent`, `POST /api/consent`, `GET|PATCH /api/session`, `POST /api/approve`, `POST /api/verify_email`, `POST /api/send_resume_url`
  - *files* — `POST|PATCH|DELETE /api/upload`, `GET /api/upload_files`, `GET /api/files`
  - *processing* — `POST /api/process` (issues the conversion_id, commits the run, then triggers the `process-session/default` deployment over the Prefect REST API; best-effort — a trigger failure is logged, not raised)
  - *monitoring* — `GET /api/progress`, `GET /api/log`, `GET /api/versions`, `GET /api/health`
  - *results* — `GET /api/download` (flips `session.downloaded`, requires `approved`), `GET /api/output_files`, `GET /api/coordinate`
  - *analysis* — `GET /api/coordinate_validation`, `GET /api/nmr_validation`, `GET /api/nmr_preview`, `GET /api/output_statistics`
- `features/auth.py` — passwordless login and annotator (admin) authentication, plus the help desk. Sign-in mail carrying **both** a magic link and a 6-digit code (one `login_challenge` row backs both, so spending either consumes the other; single-use, 15-min TTL, account created on first login, no passwords); annotators listed in `SERVICE_ANNOT_EMAILS` additionally need TOTP before admin authority is granted. The login session is server-side (`auth_session` row) behind an opaque httpOnly cookie, with a per-session CSRF token required on state-changing requests, and Redis fixed-window rate limits. Exposes `/api/auth/*`, `/api/sessions`, `/api/help/*`, and the helpers `app.py` reuses for authorization (`current_auth`, `require_csrf`, `session_by_token`, `authorize_session`, `record_admin_access`). It is registered from `app.py` via `init_auth(app, session_factory, mailer)` — the session factory and mailer are injected to break a circular import. `_establish_login()` holds the shared tail of the two ways a challenge is spent (`POST /api/auth/verify` for the link, `POST /api/auth/verify_code` for the code), so a cross-device login is indistinguishable from a same-device one. The anonymous capability-URL flow (`?token=`) is untouched; login is additive.
- `core/site_config.py` — generated site constants: service level, domain, DB URL, validity periods, `AUTH_SECRET`, `SERVICE_ANNOT_EMAILS`, `SERVICE_HELP_EMAIL`, the `PEER_*` cross-site-exchange settings, the archive/workspace base paths, the conversion-ID range, and the three image names (`MAXIT_CCD_IMAGE`, `UTILS_NMR_IMAGE`, `PDF_REPORT_IMAGE`).
- `core/models.py` — **generated** SQLAlchemy ORM models. Source of truth is `postgres/init-service.sql.template`; regenerate with [omymodels](https://archon-omymodels-online.hf.space) when the schema changes. The file also carries hand-added edits the generator drops (the `EnumStr` TypeDecorator, the `SERVICE_DOMAIN` import, the per-column comments), so re-apply them after any regeneration — for a column or two, hand-syncing is safer.

### Database Schema (`postgres/init-service.sql.template`)
Key tables:
- `session` — one row per user UI session; tracks lifecycle from `created` → `uploading` → `processing` → `completed`/`failed`/`expired`. The `conversion_id` (INT, range-partitioned by site) is assigned when processing begins and forms the public ID (displayed as `C_<conversion_id>`). Also carries `token_admin` (annotator access, audited), `user_id` (owner; NULL = anonymous), `approved`, `downloaded`, `exchanged`, and `help_user_seen_at`.
- `upload_file` — files uploaded by the user, keyed by `(token, ordinal)`. Files **accumulate** across runs and are never physically deleted once a run has used them; `selected` controls participation and `run_number` records the draft run a file was uploaded for.
- `output_file` — converted output files, keyed by `(conversion_id, run_number, ordinal)`.
- `workflow` — Prefect task tracking per conversion+run, keyed by `(conversion_id, run_number, ordinal)` (`WfTaskCode` enum: `issue_conversion`, `convert_model`, `convert_nmr_data`, `nef_release`, `convert_pdf`, `notification`, `communication`, `session_cleanup`, `session_exchange`, `session_lock`).
- `notification` / `communication` — email delivery records; `communication` also backs the help-desk threads.
- `app_user` / `login_challenge` / `auth_session` / `admin_access_audit` — the authentication tables (accounts and roles, pending sign-in mails, live login sessions, and the audit trail of annotator access to other users' sessions). `login_challenge` carries both credentials of one sign-in mail: `token_hash` for the link and `pending_id` + `code_hash` (an HMAC keyed by `AUTH_SECRET`) for the code, with `attempts` capping code guesses. Neither it nor `auth_session` is pruned anywhere except the nightly `cleanup-sessions` flow.

A session supports multiple processing runs: `session.latest_run_number` counts committed runs (0 before the first Process; the in-progress draft is always `latest_run_number + 1`), and each `POST /api/process` creates a git commit tagged `run-<N>` plus `output_file`/`workflow` rows carrying that `run_number`.

Conversion ID ranges: bmrb.io → 1000001–2000000, pdbj.org → 2000001–3000000, development → 8000001–9000000.

### Prefect flows (`prefect/flows/`)
Flows live in `prefect/flows/core/`; `prefect/flows/prefect.yaml` declares four deployments, all on `local-pool`:

| Flow | Entrypoint | Trigger |
| --- | --- | --- |
| `process-session` | `core/process_session.py` | on demand, from `POST /api/process` |
| `capture-versions` | `core/versions.py` | every 600 s — writes `versions.json`, served by `GET /api/versions` |
| `cleanup-sessions` | `core/cleanup.py` | cron `0 2 * * *` — retention purge (Terms #8): removes the archive and workspace trees, purges child rows, marks the session `expired`, purges spent `login_challenge` / `auth_session` rows, emails the admin a summary |
| `exchange-sessions` | `core/exchange.py` | cron `0 */6 * * *` — cross-site (Osaka ↔ UConn) pull of the peer's sessions over SSH + rsync. **Implemented but inert until `PEER_HOST` is set.** |

`process_session` copies the selected uploads into the run workspace, then runs coordinate conversion (maxit-ccd) and NMR data conversion (NmrDpUtility, with a different driver script per `target_depsys`), harvests `output_file` rows, sets the session terminal status, and finally runs the deferred NEF release and PDF report before deleting `work/`.

**Import constraint:** `prefect/flows/shared/core` is a symlink to `backend/app/core`, and each flow prepends its own directory plus `../shared` to `sys.path`. A flow module may therefore import the shared `core.*` package and the sibling `workspace` module, but flow modules in `core/` **cannot cross-import each other** — shared logic must go into `core.*` or be duplicated. Redeploy after editing a flow (see Commands).

### PDF / HTML report
`pdf/` builds the `bmrb-extract-pdf-report:local` image (`./pdf/build.sh`), which `process_session` invokes via `docker run`. `generate_report.py` reads the last NMR data-processing JSON report plus a small `provenance.json` the flow assembles from the DB — the container never touches the database — builds `chart_inputs.json`, shells out to `render_charts.mjs` for per-chart SVGs, and renders `templates/report.html` (Jinja2) to A4 PDF via WeasyPrint.

`pdf/build.sh` stages `frontend/src/app/pages/report-charts.ts` into the build context so esbuild can bundle it as `vendor/report-charts.mjs`. The PDF charts are therefore *the same code* as the on-screen ones: `report-charts.ts` must stay free of Angular and DOM dependencies, and editing it changes both the SPA and the PDF.

### Frontend (`frontend/src/`)
Angular 21, zoneless, standalone components only, signals throughout (`signal`/`computed`/`effect`) with `@if`/`@for`/`@defer` control flow. Every route is lazy-loaded via `loadComponent`. UI library: PrimeNG 21 (Aura preset customized in `app.config.ts`) + Tailwind CSS v4 + `tailwindcss-primeui`. Charts use ECharts 6 (lazily imported); the coordinate preview uses a prebuilt Mol* bundle served from `/molstar/`.

Routes and both guards live in `frontend/src/app.routes.ts` (at `src/`, **not** `src/app/`); page components live in `frontend/src/app/pages/`:

| Route | Guard | Component |
| --- | --- | --- |
| `''` → `info` | — | redirect |
| `info`, `preface`, `terms`, `privacy` | — | `page.info`, `page.preface`, `page.terms`, `page.privacy` |
| `upload`, `summary`, `download` | `tokenGuard` | `page.upload`, `page.summary`, `page.download` |
| `login`, `login/verify` | — | `page.login`, `page.login-verify` |
| `sessions`, `help` | `authGuard` | `page.sessions`, `page.help` |

There is no wildcard (`**`) route, so an unknown URL currently yields an empty main area rather than a 404 page.

`consent.to` is not a route — it is the consent checkbox component embedded in the `info` page. The shell (`src/app/layout/`) supplies the topbar (conversion-ID tag, dark-mode toggle, help-desk unread badge), the sidebar menu, the footer (image versions from `GET /api/versions`), and the `@defer`-loaded consent/expiry dialogs.

Shared modules under `src/app/pages/`: `file-types.ts` (the canonical `upload_file_type` catalogue plus `fileTypeLabel()`), `echart.component.ts` (the reusable `app-echart` host), and `report-charts.ts` (see *PDF / HTML report* — keep it DOM-free).

Auth on the client is `auth.service.ts` (signals for the login state, plus the `/api/auth/*`, `/api/sessions` and `/api/help/*` calls) and `auth.interceptor.ts`, which adds `withCredentials` to every `/api/` request and `X-CSRF-Token` to mutating ones.

Because the mail application opens the magic link in a *new* tab while the session cookie is set origin-wide, `auth-channel.ts` (a `BroadcastChannel` named `bmrbx_auth`) hands the login back: the verify tab announces `login`, the tab still waiting for the link (`AuthService.awaitingMagicLink`) answers `login-ack` and continues to `/sessions`, and the verify tab then tells the user to return to it. All other tabs simply re-read `/api/auth/me`. `logout` is broadcast too. Same browser only — it degrades to today's behavior where `BroadcastChannel` is unavailable.

**Cross-device login.** The mail is often read on a phone while the files (and so the login) live on a workstation, and the cookie can only be set on the device that makes the request — which `BroadcastChannel` cannot bridge. So `request_login` also returns an opaque `pending_id`, stashed in `localStorage` under `bmrbx_pending_login` and echoed in the link as `?p=`. `/login/verify` treats itself as the originating browser only when its stash matches *that* handle (matching the handle, not merely finding a stash, rules out one left by an abandoned login on the same device); otherwise it does **not** consume the challenge and tells the user to type the code back on the device they started from. `page.login` therefore shows a `p-inputOtp` code form instead of a dead end, plus Resend / different address / refresh. An escape hatch on the mail device re-verifies with `cross_device: true`, which the backend refuses for annotators (403 `annotator_must_use_code`, challenge left unused) so their TOTP step always lands on the workstation. Guessing is bounded by `login_challenge.attempts` (max 5, then the challenge is burnt), not by rate limiting — note `request.remote_addr` is the nginx container IP, so every per-IP limit in `auth.py` is really a global throttle. **No polling was added**: the code path is user-driven, and gunicorn runs one sync worker.

The upload page queries `https://api.bmrb.io/v2` **directly from the browser** for BMRB-ID validation — that traffic is not proxied through `/api/`, so it depends on that host's availability and CORS.

Site-specific content (branding, URLs, help email) is injected via the symlinked `site.config.ts` (generated from `bmrb.config.ts.template` or `bmrbj.config.ts.template`; the symlink and both generated variants are git-ignored). Besides the branding constants it exports `SERVICE_LEVEL`, `FRONTEND_VERSION`, the validity periods, and `API_URL = '/api/'` — the browser-facing Flask API base path (proxied by nginx). Do not use the Docker-internal `FLASK_API_URL` env var in Angular code.

### Session token flow
After the user consents on `/info`, `PageService.newConsent()` calls `POST /api/new_consent`, receives a UUIDv7 token, and navigates to `/info?token=<uuid>`. Most navigation uses `queryParamsHandling: 'preserve'` so the token stays in the URL; the account pages (`/sessions`, `/help`) deliberately do not. `PageService` restores session state from `GET /api/session` on initial load **and on every `NavigationEnd`**, so opening a session from "My sessions" or a resume link works without a reload.

`tokenGuard` protects `upload`, `summary`, and `download`. It does not simply redirect:
- **No `?token=` in the URL** — if a session token is still held in memory (the user is coming back from an account page), it re-attaches the token to the same path and redirects there; otherwise it sets `PageService.consentRequired`, which `AppLayout` renders as a "Consent Required" dialog.
- **Token present** — a cached `tokenValidation` signal (`valid` / `expired` / `invalid`) short-circuits repeat navigations; consent is re-read live so unchecking the box blocks immediately. On the first navigation or a fresh reload it validates against `GET /api/session`, and an expired session raises the "Session Expired" dialog.

`authGuard` protects `sessions` and `help`; it redirects to `/login` when `GET /api/auth/me` reports no login session.

A logged-in user may adopt an anonymous session into their account (`POST /api/auth/claim_session`); the pending token survives the magic-link round-trip in `localStorage`, so it works even when the link is opened on another device.
