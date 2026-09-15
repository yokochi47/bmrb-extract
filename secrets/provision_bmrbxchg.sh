#!/bin/bash
#
# Provision the unprivileged peer-exchange account on this site, so the PEER
# site can pull our sessions (see secrets/README.md, "Peer-side prerequisites").
#
# One-time root step per site, deliberately kept out of setup.sh: it creates a
# system account and a DB role, neither of which belongs in the boot path.
# Idempotent — re-run after ./config.sh or ./reset_db.sh (the latter drops every
# table, and table grants die with their tables, so step 5 re-applies them).
# Run from anywhere; paths are resolved relative to this script.
#
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Error: run as root (sudo bash $0)." >&2; exit 1; }

here="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$here/../.env"

if [[ ! -e "${ENV_FILE}" ]] ; then
  echo "Error: Missing .env file. Run ./config.sh first."
  exit 1
fi

# .env carries `export POSTGRES_USER=$USER`, i.e. it resolves to whoever sources
# it -- and this script runs under sudo, where that is `root`. Pin USER to the
# invoking account first, so every $USER-derived value matches the deploy user's.
USER="${SUDO_USER:-${USER:-}}"

source "${ENV_FILE}"

# The account created HERE is the one the peer logs into. It is NOT
# ${PEER_SSH_USER}: that names the account on the *peer* side (config.sh prompts
# for it), which may well differ. Override on the command line if needed.
ACCOUNT=${1:-bmrbxchg}
DB=${POSTGRES_SERVICE_DB:?not set in .env -- run ./config.sh first}
CONTAINER=bmrb-extract-postgres

if ! command -v docker > /dev/null ; then
  echo "Error: Docker is not installed."
  exit 1
fi

if [[ -z "$(docker ps -q -f "name=^${CONTAINER}$")" ]] ; then
  echo "Error: container ${CONTAINER} is not running. Run ./start.sh first."
  exit 1
fi

# Ask the container which superuser it was initialized with: authoritative, and
# immune to the $USER caveat above. Fall back to .env for an older container
# started without the variable in its environment.
PGUSER_ADMIN=$(docker exec "$CONTAINER" printenv POSTGRES_USER 2>/dev/null || true)
PGUSER_ADMIN=${PGUSER_ADMIN:-${POSTGRES_USER:?not set in .env -- run ./config.sh first}}

#
# 1. Unprivileged account -- deliberately NOT in sudo or docker.
#
if id "$ACCOUNT" &>/dev/null ; then
  echo "[1/6] account $ACCOUNT already exists, leaving it alone"
elif command -v adduser > /dev/null ; then
  adduser --disabled-password --gecos '' --shell /bin/bash "$ACCOUNT"
  echo "[1/6] created $ACCOUNT"
else
  useradd --create-home --shell /bin/bash "$ACCOUNT"
  passwd --lock "$ACCOUNT" > /dev/null
  echo "[1/6] created $ACCOUNT"
fi

HOME_DIR=$(getent passwd "$ACCOUNT" | cut -d: -f6)

#
# 2. SSH skeleton. The peer's PUBLIC key goes in authorized_keys, prefixed with
#    the `restrict` option -- see the manual steps at the end.
#
install -d -m 700 -o "$ACCOUNT" -g "$ACCOUNT" "$HOME_DIR/.ssh"
touch "$HOME_DIR/.ssh/authorized_keys"
chmod 600 "$HOME_DIR/.ssh/authorized_keys"
chown "$ACCOUNT:$ACCOUNT" "$HOME_DIR/.ssh/authorized_keys"
echo "[2/6] ~/.ssh ready (authorized_keys is empty until the peer sends its key)"

#
# 3. Host psql client -- postgres only listens inside the container, and giving
#    this account the docker group would make it root-equivalent.
#
if command -v psql &>/dev/null ; then
  echo "[3/6] psql already present: $(psql --version)"
else
  apt-get update -qq && apt-get install -y -qq postgresql-client
  echo "[3/6] installed $(psql --version)"
fi

#
# 4. Read-only DB role. The password is generated here and never leaves this
#    host; on a re-run it is read back out of ~/.pgpass so the peer's working
#    credentials survive. Keep the generated alphabet alphanumeric: a ':' would
#    break both the .pgpass format and the read-back below.
#
PGPASS_FILE="$HOME_DIR/.pgpass"
NEW_PGPASS=

if [[ -f "$PGPASS_FILE" ]] ; then
  PW=$(awk -F: 'NR==1 {print $5}' "$PGPASS_FILE")
  if [[ -z "$PW" ]] ; then
    echo "Error: $PGPASS_FILE exists but holds no password. Remove it and re-run."
    exit 1
  fi
  echo "[4/6] reusing the password in $PGPASS_FILE"
else
  PW=$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c 32)
  NEW_PGPASS=1
  echo "[4/6] generated a new password for role $ACCOUNT"
fi

#
# 5. Role + grants, re-applied on every run. COPY (SELECT ...) TO STDOUT needs
#    nothing beyond SELECT, and only on the four tables exchange.py reads --
#    keeping the peer account away from app_user / login_challenge /
#    auth_session / admin_access_audit. The REVOKEs converge sites provisioned
#    with the earlier blanket 'ALL TABLES' grant onto this narrower set; add a
#    table here if prefect/flows/core/exchange.py ever reads one.
#
docker exec -i "$CONTAINER" psql -U "$PGUSER_ADMIN" -d "$DB" -q -v ON_ERROR_STOP=1 <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$ACCOUNT') THEN
    CREATE ROLE $ACCOUNT LOGIN;
  END IF;
END \$\$;
ALTER ROLE $ACCOUNT LOGIN PASSWORD '$PW';
GRANT CONNECT ON DATABASE $DB TO $ACCOUNT;
GRANT USAGE ON SCHEMA public TO $ACCOUNT;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE SELECT ON TABLES FROM $ACCOUNT;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM $ACCOUNT;
GRANT SELECT ON session, upload_file, output_file, workflow TO $ACCOUNT;
SQL
echo "[5/6] role $ACCOUNT granted SELECT on session, upload_file, output_file, workflow"

if [[ -n "$NEW_PGPASS" ]] ; then
  ( umask 077; printf '127.0.0.1:5432:%s:%s:%s\n' "$DB" "$ACCOUNT" "$PW" > "$PGPASS_FILE" )
  chmod 600 "$PGPASS_FILE"; chown "$ACCOUNT:$ACCOUNT" "$PGPASS_FILE"
  echo "      wrote $PGPASS_FILE (postgres is published on 127.0.0.1:5432, see compose.yml)"
fi
unset PW

#
# 6. Verify, as the account itself: it can log in and read, cannot write, holds
#    no extra groups, and can read the two trees the peer rsyncs. Every check is
#    a query that SUCCEEDS and reports a boolean, so nothing here can leave a
#    stray object behind or trip `set -e` on an expected denial.
#
as_account() {
  local out
  if out=$(sudo -u "$ACCOUNT" -H psql -h 127.0.0.1 -U "$ACCOUNT" -d "$DB" -tAqc "$1" 2>&1) ; then
    echo "${out:-<empty>}"
  else
    echo "FAILED -- $(echo "$out" | tail -1)"
  fi
}

readable() {
  if [[ ! -e "$1" ]] ; then
    echo "missing ($1) -- created on first upload"
  elif sudo -u "$ACCOUNT" -H test -r "$1" && sudo -u "$ACCOUNT" -H test -x "$1" ; then
    echo "ok ($1)"
  else
    echo "NOT READABLE ($1) -- the rsync half of the exchange will fail"
  fi
}

echo "[6/6] verification"
echo "  groups:        $(id -nG "$ACCOUNT")"
echo "  read test:     $(as_account 'SELECT count(*) FROM session')"
echo "  can INSERT:    $(as_account "SELECT has_table_privilege(current_user,'session','INSERT')") (want f)"
echo "  can CREATE:    $(as_account "SELECT has_schema_privilege(current_user,'public','CREATE')") (want f)"
echo "  sees app_user: $(as_account "SELECT CASE WHEN to_regclass('public.app_user') IS NULL THEN 'n/a' ELSE has_table_privilege(current_user,'app_user','SELECT')::text END") (want f)"
echo "  archive tree:  $(readable "${ARCHIVE_VOL_DIR:-/var/lib/archive}")"
echo "  workspace:     $(readable "${WORKSPACE_VOL_DIR:-/var/lib/workspace}")"

cat <<MSG

Remaining manual steps, once the PEER site sends their public key:
  1. echo 'restrict <their-public-key>' >> $HOME_DIR/.ssh/authorized_keys
  2. open port 22 to their IP only in the firewall
  3. tell them to answer ./config.sh with:
       peer SSH account:  $ACCOUNT
       peer psql command: psql -h 127.0.0.1 -U $ACCOUNT -d $DB
MSG
