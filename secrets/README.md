# secrets/

Host directory mounted read-write into the `prefect-worker` container at
`/secrets` (see `compose.yml`). Holds the credentials for the **cross-site data
exchange** flow (`prefect/flows/core/exchange.py`). Contents are gitignored
(only `.gitkeep`, this README and `provision_bmrbxchg.sh` are tracked), so the
directory always exists and the compose mount is safe even before the key is
provisioned.

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
over root.

### 1. Run the provisioning script

On the site being read *from*, with the stack already up:

```bash
sudo bash secrets/provision_bmrbxchg.sh     # optional argument: a different account name
```

It is idempotent — re-run it after `./config.sh` and **after `./reset_db.sh`**,
which drops every service table and takes the table grants down with them (the
role itself is cluster-level and survives, so without a re-run the account looks
fine and every peer query fails with "permission denied"). A re-run reads the
existing password back out of `~/.pgpass` rather than rotating it.

The script creates, on this host:

1. The `bmrbxchg` system account — no `sudo`, no `docker` group — with `~/.ssh`
   (0700) and an empty `authorized_keys` (0600).
2. A host `postgresql-client`. Postgres only listens inside the container, and
   `docker exec ... psql` would need the docker group, which defeats the point
   of an unprivileged account; the published `127.0.0.1:5432` (see
   `compose.yml`) is there for exactly this. An older client talks to the 18
   server fine.
3. A read-only DB role with a locally generated password, and matching
   `~bmrbxchg/.pgpass` (0600). `COPY (SELECT ...) TO STDOUT` needs nothing
   beyond `SELECT`, and only on the four tables `exchange.py` actually reads —
   the auth tables (`app_user`, `login_challenge`, `auth_session`,
   `admin_access_audit`) stay out of the peer's reach:

   ```sql
   -- applied on every run; the REVOKEs converge a site provisioned with an
   -- earlier blanket "ALL TABLES IN SCHEMA public" grant onto this narrower set
   ALTER ROLE bmrbxchg LOGIN PASSWORD '<generated>';
   GRANT CONNECT ON DATABASE internal TO bmrbxchg;
   GRANT USAGE ON SCHEMA public TO bmrbxchg;
   ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE SELECT ON TABLES FROM bmrbxchg;
   REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM bmrbxchg;
   GRANT SELECT ON session, upload_file, output_file, workflow TO bmrbxchg;
   ```

   This is a role, not schema DDL, so `postgres/init-service.sql.template` is
   untouched. Add a table to that `GRANT` if the exchange ever reads one.

It then verifies the account: the read works, `INSERT` and `CREATE` are denied,
`app_user` is invisible, no extra groups, and both storage trees are readable.
The trees need no ACL work — they are already world-readable (mode 0755/0644,
written by the containers as root) — but a permission regression there breaks
the rsync half of the exchange, so it is checked here rather than discovered by a
failed 6-hourly flow run.

### 2. Finish by hand

The script prints these as reminders; they need the peer's input or root on the
firewall:

1. The peer's **public** key in `~bmrbxchg/.ssh/authorized_keys`, prefixed with
   the `restrict` option — it disables pty and agent/TCP/X11 forwarding while
   still allowing the piped `psql` and the two `rsync` commands. A `command=`
   forced command needs a dispatcher wrapper (three distinct remote command
   shapes), so it is not used by default.
2. Firewall: allow `PEER_SSH_PORT` from the peer's IP only.
3. Tell the peer to answer the `config.sh` prompts with `bmrbxchg` and
   `psql -h 127.0.0.1 -U bmrbxchg -d internal`.
