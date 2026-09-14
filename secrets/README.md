# secrets/

Host directory mounted read-write into the `prefect-worker` container at
`/secrets` (see `compose.yml`). Holds the credentials for the **cross-site data
exchange** flow (`prefect/flows/core/exchange.py`). Contents are gitignored
(only `.gitkeep` and this README are tracked), so the directory always exists and
the compose mount is safe even before the key is provisioned.

Provision these on **each** site when the peer's global IP is known:

- `peer_ssh_key` — the **private** SSH key this site uses to `ssh`/`rsync` into the
  peer. Must be `chmod 600` (ssh refuses lax permissions). Its matching public key
  goes into the peer's SSH account — a dedicated unprivileged one with a
  `restrict`ed authorized key (see *Peer-side prerequisites* below).
- `peer_known_hosts` — the peer host key(s). Left empty, the flow uses
  `StrictHostKeyChecking=accept-new` and appends the key on first connect; pre-pin
  it for stricter security.

Then run `./config.sh`, which prompts for `PEER_HOST` (peer host/IP) and — once
that is non-empty — for `PEER_SSH_USER` (default `bmrbxchg`) and `PEER_PSQL`
(default `psql -d internal`). Those last two describe the **peer** side and so
differ per site: never hand-edit them in `.env`, which `config.sh` regenerates
from the tracked `.env.template` on every run. Then rebuild the worker
(`openssh-client` + `rsync` are baked into `prefect/Dockerfile`) and re-register
the deployment:
`docker compose exec prefect-worker sh -c "cd /flows && prefect deploy --all"`.

## Peer-side prerequisites

What the **other** site must provision so this one can pull (and vice versa): an
SSH account reachable on `PEER_SSH_PORT`, allowed through the firewall from this
site's IP, able to (a) `rsync`-read `/var/lib/archive` and `/var/lib/workspace`,
and (b) run `PEER_PSQL` read-only against its own `internal` database (the
exchange SQL is piped to it on stdin). Expect roughly `4 + 2 x sessions`
short-lived SSH connections per run — four peer DB reads plus one archive and
one workspace rsync per session.

Use a dedicated unprivileged account, **not** the deployment user: the latter is
in `sudo` and `docker`, so authorizing the peer's key there effectively hands
over root. The recipe, run on the site being read *from*:

1. `sudo adduser --disabled-password --gecos '' bmrbxchg` — no `sudo`, no
   `docker` group.
2. The peer's **public** key in `/home/bmrbxchg/.ssh/authorized_keys` (0600, dir
   0700), prefixed with the `restrict` option — it disables pty and
   agent/TCP/X11 forwarding while still allowing the piped `psql` and the two
   `rsync` commands. A `command=` forced command needs a dispatcher wrapper
   (three distinct remote command shapes), so it is not used by default.
3. Firewall: allow `PEER_SSH_PORT` from the peer's IP only.
4. Both storage trees are already world-readable (mode 0755/0644, written by the
   containers as root), so no ACL work is needed for the rsync half.
5. The DB half needs a client on the host — postgres only listens inside the
   container. `docker exec -i bmrb-extract-postgres psql ...` would work but
   requires the docker group, which defeats (1); install a host client instead
   (`postgresql-client-17` talks to the 18 server fine) and create a read-only
   role — `COPY (SELECT ...) TO STDOUT` needs nothing beyond `SELECT`:

   ```sql
   CREATE ROLE bmrbxchg LOGIN PASSWORD '<generated>';
   GRANT CONNECT ON DATABASE internal TO bmrbxchg;
   GRANT USAGE ON SCHEMA public TO bmrbxchg;
   GRANT SELECT ON ALL TABLES IN SCHEMA public TO bmrbxchg;
   ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO bmrbxchg;
   ```

   Apply with `docker exec -i bmrb-extract-postgres psql -U "$POSTGRES_USER" -d
   "$POSTGRES_SERVICE_DB"`. This is a role, not schema DDL, so
   `postgres/init-service.sql.template` is untouched — but it must be re-applied
   after `./reset_db.sh`.
6. `~bmrbxchg/.pgpass` (0600, owned by `bmrbxchg`) holding
   `127.0.0.1:5432:internal:bmrbxchg:<generated>`.
7. Tell the peer to answer the `config.sh` prompts with `bmrbxchg` and
   `psql -h 127.0.0.1 -U bmrbxchg -d internal`.
