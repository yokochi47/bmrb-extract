#!/bin/sh
#
# Render core/site_config.py from the environment, at container start.
#
# Upstream generates this file on the host with config.sh + envsubst. We run the
# exact same substitution over the exact same upstream template, but inside the
# container on every start — so the module tracks whatever upstream adds to the
# template, and every value is re-read when the stack restarts.
#
# A block of local-only settings is appended afterwards: things the upstream
# template hardcodes (the maxit memory cap, image tags, the BMRB FTP base) plus
# the extensions the local deployment needs (full SMTP configuration, an
# explicit public base URL, an explicit cookie policy).
#
# Usage: render-site-config <output-path>

set -eu

TEMPLATE="${SITE_CONFIG_TEMPLATE:-/opt/bmrb-extract/site_config.py.template}"
TARGET="${1:?usage: render-site-config <output-path>}"

if [ ! -f "$TEMPLATE" ]; then
    echo "render-site-config: missing template $TEMPLATE" >&2
    exit 1
fi

# Fail loudly on an unset value rather than emitting a site_config.py that dies
# with a SyntaxError or, worse, silently configures the wrong thing.
missing=
for var in SERVICE_LEVEL SERVICE_DOMAIN SERVICE_HOST SERVICE_ADMIN_EMAIL \
           SERVICE_HELP_EMAIL AUTH_SECRET CONV_ID_RANGE_BEGIN CONV_ID_RANGE_END \
           SERVICE_DATABASE_URL FLASK_API_URL SUCCESS_VALIDITY_PERIOD_IN_DAYS \
           FAILURE_VALIDITY_PERIOD_IN_DAYS ARCHIVE_BASE_PATH WORKSPACE_BASE_PATH \
           MAXIT_CCD_IMAGE UTILS_NMR_IMAGE; do
    eval "value=\${$var:-}"
    [ -n "$value" ] || missing="$missing $var"
done

if [ -n "$missing" ]; then
    echo "render-site-config: these variables are unset or empty in .env:$missing" >&2
    exit 1
fi

# .env composes a few values out of others (SERVICE_DATABASE_URL from the
# POSTGRES_* settings, the data paths from BMRBX_DATA_DIR). Compose resolves
# those before they reach us; if one arrives with the reference intact, the
# resolution did not happen and everything downstream would fail obscurely.
unresolved=
for var in SERVICE_DATABASE_URL FLASK_API_URL ARCHIVE_VOL_DIR WORKSPACE_VOL_DIR \
           SERVICE_BASE_URL SERVICE_HOST; do
    eval "value=\${$var:-}"
    case "$value" in
        *'${'*) unresolved="$unresolved $var" ;;
    esac
done

if [ -n "$unresolved" ]; then
    echo "render-site-config: these values still contain an unresolved \${...}" >&2
    echo "  reference:$unresolved" >&2
    echo "  Compose did not expand them. Replace the references in .env with" >&2
    echo "  literal values." >&2
    exit 1
fi

# The archive and workspace host paths are handed straight to `docker run -v`
# by the conversion tasks, and the daemon reads them relative to the HOST. A
# relative path there silently becomes a named volume instead of the directory
# the user meant, so refuse to start.
for var in ARCHIVE_VOL_DIR WORKSPACE_VOL_DIR; do
    eval "value=\${$var:-}"
    case "$value" in
        ''|/*) ;;
        *)
            echo "render-site-config: $var must be an absolute host path (got '$value')." >&2
            echo "  Set BMRBX_DATA_DIR in .env to a literal absolute path." >&2
            exit 1
            ;;
    esac
done

mkdir -p "$(dirname "$TARGET")"
envsubst < "$TEMPLATE" > "$TARGET"

cat >> "$TARGET" <<'PYEOF'

# ---------------------------------------------------------------------------
# Local deployment settings, appended by docker/common/render-site-config.sh.
#
# These re-read the environment at import time so a restart is enough to change
# them. Anything the upstream template already defines is used as the default,
# which keeps this block additive.
# ---------------------------------------------------------------------------

import os as _os


def _env(name, default=''):
    value = _os.environ.get(name)
    return default if value is None or value == '' else value


def _flag(name, default=False):
    return str(_env(name, 'true' if default else 'false')).strip().lower() \
        in ('1', 'true', 'yes', 'on')


# Public base URL used to build the links people receive by mail (magic-link
# login, "send this URL to my address"). Upstream hardcodes https://<host>,
# which is wrong for an instance served over plain HTTP or on a non-default port.
SERVICE_BASE_URL = _env('SERVICE_BASE_URL', 'https://' + SERVICE_HOST).rstrip('/')

# Whether the login cookie carries the Secure attribute. Browsers withhold a
# Secure cookie on http:// origins, so a plain-HTTP instance must set this
# false or logins never stick.
AUTH_COOKIE_SECURE = _flag('AUTH_COOKIE_SECURE', SERVICE_LEVEL == 'production')

# Outgoing mail. Upstream assumes an unauthenticated internal relay on port 25;
# these let the same code reach an authenticated submission service.
# MAIL_BACKEND='log' skips delivery and writes the message to the process log.
MAIL_BACKEND = _env('MAIL_BACKEND', 'smtp')
SMTP_PORT = int(_env('SMTP_PORT', '25'))
SMTP_USER = _env('SMTP_USER')
SMTP_PASSWORD = _env('SMTP_PASSWORD')
SMTP_STARTTLS = _flag('SMTP_STARTTLS', False)
SMTP_SSL = _flag('SMTP_SSL', False)
SMTP_FROM = _env('SMTP_FROM', SERVICE_ADMIN_EMAIL)
SMTP_TIMEOUT = int(_env('SMTP_TIMEOUT', '30'))

# Values the upstream template hardcodes, made configurable.
MAXIT_MEMORY_LIMIT = _env('MAXIT_MEMORY_LIMIT', MAXIT_MEMORY_LIMIT)
PDF_REPORT_IMAGE = _env('PDF_REPORT_IMAGE', PDF_REPORT_IMAGE)
BMRB_ENTRY_DIR_URL = _env('BMRB_ENTRY_DIR_URL', BMRB_ENTRY_DIR_URL)
PYEOF

# Execute the rendered module so a bad value fails HERE, with the offending
# line in view, instead of halfway through a gunicorn boot or a flow run. This
# also runs the template's own assertions (service level, conversion-ID range).
if ! python3 -c "import runpy, sys; runpy.run_path(sys.argv[1])" "$TARGET"; then
    echo "render-site-config: the rendered $TARGET is not valid — see the error above." >&2
    echo "  Check the SERVICE_* and CONV_ID_RANGE_* values in .env." >&2
    exit 1
fi

echo "render-site-config: wrote $TARGET"
