#!/usr/bin/env bash
#
# Check this branch against upstream for the drift that would NOT show up as a
# merge conflict.
#
# Two kinds of check:
#
#   1. Baseline drift — upstream files this branch shadows or reads the shape
#      of, which changed since the last reconciliation. Git will not flag these,
#      because this branch does not edit them.
#
#   2. Wiring — values that must appear in several places at once (a variable in
#      a template, in compose.yaml and in .env.local.template) and go silently
#      empty when one is missed.
#
# Run it after every `git merge main`. See MERGING_UPSTREAM.md for what to do
# about each finding.
#
# Usage:  docker/check-upstream-drift.sh [upstream-ref]     (default: main)

set -uo pipefail

cd "$(dirname "$0")/.."

UPSTREAM_REF="${1:-main}"
BASELINE_FILE=docker/upstream-baseline

red=$'\033[31m'; green=$'\033[32m'; yellow=$'\033[33m'; dim=$'\033[2m'; off=$'\033[0m'
[ -t 1 ] || { red=; green=; yellow=; dim=; off=; }

findings=0

pass() { printf '%s  ok  %s%s\n' "$green" "$1" "$off"; }
warn() { printf '%s  !!  %s%s\n' "$yellow" "$1" "$off"; findings=$((findings + 1)); }
note() { printf '%s      %s%s\n' "$dim" "$1" "$off"; }
head_() { printf '\n%s\n' "$1"; }

# --------------------------------------------------------------------------
# 1. Upstream files this branch depends on but does not edit
# --------------------------------------------------------------------------
# path | what breaks here when it changes upstream
WATCHED=$(cat <<'EOF'
backend/app/core/site_config.py.template|rendered at container start by docker/common/render-site-config.sh
postgres/setup-service.sql.template|rendered at first start by docker/postgres/initdb/10-bmrb-extract.sh
postgres/init-service.sql.template|same, and docker/postgres/initdb patches the processing_site enum in it
postgres/init-prefect.sql.template|same
frontend/src/bmrb.config.ts.template|shadowed by docker/frontend/site.config.local.ts
frontend/src/bmrbj.config.ts.template|shadowed by docker/frontend/site.config.local.ts
frontend/src/index.bmrb.html|shadowed by docker/frontend/index.local.html
frontend/angular.json|docker/nginx/Dockerfile copies dist/bmrb_extract/browser out of the build stage
nginx/nginx-production.conf.template|shadowed by docker/nginx/bmrb-extract.conf.template
nginx/nginx-development.conf.template|shadowed by docker/nginx/bmrb-extract.conf.template
nginx/share/401.html|copied into docker/nginx/Dockerfile
nginx/share/403.html|copied into docker/nginx/Dockerfile
pdf/Dockerfile|forked as docker/pdf/Dockerfile
pdf/build.sh|its staging step is inlined into docker/pdf/Dockerfile
pdf/package.json|docker/pdf/Dockerfile runs npm ci + bundle-charts against it
compose.yml|shadowed by compose.yaml
README.md|rewritten on this branch and pinned with merge=ours, so upstream edits are DISCARDED, not merged
.env.template|shadowed by .env.local.template
prefect/flows/prefect.yaml|baked into the worker image; deployments registered by its entrypoint
prefect/flows/shared/core|symlink target fixes the /flows + /backend/app layout in docker/prefect/Dockerfile
backend/requirements.txt|installed by docker/backend/Dockerfile
setup.sh|its host-side steps were replaced; check whether a NEW one matters
EOF
)

head_ "Upstream files this branch shadows or reads, changed since the baseline"

if [ ! -f "$BASELINE_FILE" ]; then
    warn "no $BASELINE_FILE — cannot tell what has already been reconciled"
    note "create it with:  git rev-parse $UPSTREAM_REF > $BASELINE_FILE"
elif ! git rev-parse --verify --quiet "$UPSTREAM_REF" >/dev/null; then
    warn "upstream ref '$UPSTREAM_REF' does not exist"
else
    baseline=$(grep -vE '^\s*(#|$)' "$BASELINE_FILE" | head -1 | tr -d '[:space:]')
    if ! git rev-parse --verify --quiet "$baseline^{commit}" >/dev/null; then
        warn "baseline commit '$baseline' is not in this repository"
    else
        changed=$(git diff --name-only "$baseline".."$UPSTREAM_REF" 2>/dev/null)
        any=0
        while IFS='|' read -r path why; do
            [ -n "$path" ] || continue
            if printf '%s\n' "$changed" | grep -qxF "$path"; then
                warn "$path"
                note "$why"
                note "review:  git diff $baseline..$UPSTREAM_REF -- $path"
                any=1
            fi
        done <<< "$WATCHED"
        if [ "$any" -eq 0 ]; then
            pass "none of the watched files changed since $(git rev-parse --short "$baseline")"
        fi
    fi
fi

# --------------------------------------------------------------------------
# 2. site_config.py.template variables must reach the containers
# --------------------------------------------------------------------------
head_ "site_config.py.template placeholders wired through compose.yaml"

missing=
for v in $(grep -oE '\$\{[A-Za-z_][A-Za-z0-9_]*\}' backend/app/core/site_config.py.template \
           | tr -d '${}' | sort -u); do
    grep -qF "\${$v}" compose.yaml || missing="$missing $v"
done
if [ -n "$missing" ]; then
    warn "not passed to the backend/worker:$missing"
    note "each renders as an empty string in core/site_config.py"
    note "add to the x-site-env anchor in compose.yaml and to .env.local.template"
else
    pass "every placeholder is referenced in compose.yaml"
fi

# --------------------------------------------------------------------------
# 3. compose.yaml variables must be declared in .env.local.template
# --------------------------------------------------------------------------
head_ "compose.yaml variables declared in .env.local.template"

missing=
for v in $(grep -oE '\$\{[A-Za-z_][A-Za-z0-9_]*\}' compose.yaml | tr -d '${}' | sort -u); do
    grep -qE "^${v}=" .env.local.template || missing="$missing $v"
done
if [ -n "$missing" ]; then
    warn "referenced but never declared:$missing"
    note "Compose substitutes an empty string and carries on"
else
    pass "every compose.yaml variable has a documented default"
fi

# --------------------------------------------------------------------------
# 4. init SQL placeholders must reach the postgres container
# --------------------------------------------------------------------------
head_ "init SQL placeholders wired to the postgres service"

pg_env=$(sed -n '/^  postgres:/,/^  redis:/p' compose.yaml)
missing=
for v in $(cat postgres/setup-service.sql.template \
               postgres/init-service.sql.template \
               postgres/init-prefect.sql.template \
           | grep -oE '\$\{[A-Za-z_][A-Za-z0-9_]*\}' | tr -d '${}' | sort -u); do
    printf '%s' "$pg_env" | grep -qF "\${$v}" || missing="$missing $v"
done
if [ -n "$missing" ]; then
    warn "not in the postgres service environment:$missing"
    note "the DDL would be applied with those values blank, on first start only"
else
    pass "every init SQL placeholder is passed to postgres"
fi

# --------------------------------------------------------------------------
# 5. frontend config surface
# --------------------------------------------------------------------------
head_ "frontend site-config exports and their runtime payload"

exports_of() { grep -oE 'export const [A-Za-z_][A-Za-z0-9_]*' "$1" | awk '{print $3}' | sort -u; }

upstream_exports=$(exports_of frontend/src/bmrb.config.ts.template)
local_exports=$(exports_of docker/frontend/site.config.local.ts)

only_upstream=$(comm -23 <(printf '%s\n' "$upstream_exports") <(printf '%s\n' "$local_exports") | tr '\n' ' ')
if [ -n "${only_upstream// /}" ]; then
    warn "exported upstream but missing from docker/frontend/site.config.local.ts: $only_upstream"
    note "the nginx image build fails on this — loud, but fix it here first"
else
    pass "site.config.local.ts exports everything the upstream template does"
fi

# API_URL is a constant, not a substituted value.
missing=
for v in $(printf '%s\n' "$local_exports" | grep -v '^API_URL$'); do
    grep -qE -- "--arg +$v " docker/nginx/entrypoint.d/40-bmrb-extract-frontend.sh || missing="$missing $v"
done
if [ -n "$missing" ]; then
    warn "exported but never substituted at runtime:$missing"
    note "these silently keep their compiled-in fallback"
    note "add to the jq payload in docker/nginx/entrypoint.d/40-bmrb-extract-frontend.sh,"
    note "to the nginx service environment in compose.yaml, and to .env.local.template"
else
    pass "every runtime-configurable export is in the substitution payload"
fi

# --------------------------------------------------------------------------
# 6. call sites that must go through the local shims
# --------------------------------------------------------------------------
head_ "call sites that must route through the local shims"

stray=$(grep -rn 'smtplib\.SMTP' backend/app prefect/flows --include='*.py' 2>/dev/null \
        | grep -v 'core/local_mail.py')
if [ -n "$stray" ]; then
    warn "direct smtplib use outside core/local_mail.py:"
    printf '%s\n' "$stray" | sed 's/^/        /'
    note "it would ignore SMTP_PORT/auth/TLS and MAIL_BACKEND, and hit port 25 unauthenticated"
    note "replace with:  from core.local_mail import send_message"
else
    pass "all mail goes through core.local_mail"
fi

stray=$(grep -rn "https://{SERVICE_HOST}" backend/app prefect/flows --include='*.py' 2>/dev/null \
        | grep -v "getattr(_sc, 'SERVICE_BASE_URL'")
if [ -n "$stray" ]; then
    warn "URL built from SERVICE_HOST without SERVICE_BASE_URL:"
    printf '%s\n' "$stray" | sed 's/^/        /'
    note "the link would say https:// on a plain-HTTP instance and drop the port"
else
    pass "user-facing URLs use SERVICE_BASE_URL"
fi

# --------------------------------------------------------------------------
# 7. the one piece of per-clone git configuration this branch needs
# --------------------------------------------------------------------------
head_ "merge=ours driver configured for this clone"

if ! grep -qE '^\s*README\.md\s+merge=ours' .gitattributes 2>/dev/null; then
    warn ".gitattributes no longer pins README.md with merge=ours"
    note "upstream edits to their README will come back as whole-file conflicts"
elif [ "$(git config --get merge.ours.driver)" = "true" ]; then
    pass "merge.ours.driver is set"
else
    warn "merge.ours.driver is not set in this clone"
    note "README.md will conflict on merge instead of keeping this branch's version"
    note "fix:  git config merge.ours.driver true"
fi

# --------------------------------------------------------------------------
head_ "----------------------------------------------------------------"
if [ "$findings" -eq 0 ]; then
    printf '%sNo drift found.%s\n' "$green" "$off"
    note "after reconciling a merge, record it:"
    note "  git rev-parse $UPSTREAM_REF > $BASELINE_FILE"
    exit 0
fi
printf '%s%d finding(s) — see MERGING_UPSTREAM.md.%s\n' "$red" "$findings" "$off"
exit 1
