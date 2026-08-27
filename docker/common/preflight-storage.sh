#!/bin/sh
#
# Fail fast, and legibly, when the storage this container was given is not
# actually writable by the user it runs as.
#
# The failure this exists to explain: on an NFS share exported with the usual
# root_squash, a container running as root is mapped to `nobody` and cannot
# write into a directory owned by someone else. Without this check that surfaces
# much later as a confusing permission error from PostgreSQL, git or a flow.
#
# Usage: preflight-storage <dir> [<dir> ...]

set -eu

failed=

for dir in "$@"; do
    [ -n "$dir" ] || continue

    if [ ! -d "$dir" ]; then
        if ! mkdir -p "$dir" 2>/dev/null; then
            echo "preflight: $dir does not exist and cannot be created" >&2
            failed=1
            continue
        fi
    fi

    probe="$dir/.write-probe.$$"
    if ! (: > "$probe") 2>/dev/null; then
        owner=$(stat -c '%u:%g mode %a' "$dir" 2>/dev/null || echo 'unknown')
        echo "preflight: $dir is not writable" >&2
        echo "           running as $(id -u):$(id -g), directory is $owner" >&2
        failed=1
        continue
    fi
    rm -f "$probe"
done

if [ -n "$failed" ]; then
    cat >&2 <<'EOF'

The container cannot write to its storage. The usual causes:

  * The host directory is owned by someone else. Set HOST_UID / HOST_GID in
    .env to the owner (`id -u` / `id -g`) and rebuild:
        docker compose up -d --build

  * The directory does not exist yet and the Docker daemon could not create it.
    Create it yourself, which works even where the daemon's root is squashed:
        mkdir -p "$BMRBX_DATA_DIR"/{postgres,redis,archive,workspace}

  * The share is exported read-only, or squashes all users, not just root.

EOF
    exit 1
fi
