# secrets/

Host directory mounted read-write into the `prefect-worker` container at
`/secrets` (see `compose.yml`). Holds the credentials for the **cross-site data
exchange** flow (`prefect/flows/core/exchange.py`). Contents are gitignored
(only `.gitkeep` and this README are tracked), so the directory always exists and
the compose mount is safe even before the key is provisioned.

Provision these on **each** site when the peer's global IP is known:

- `peer_ssh_key` — the **private** SSH key this site uses to `ssh`/`rsync` into the
  peer. Must be `chmod 600` (ssh refuses lax permissions). Its matching public key
  goes into the peer's SSH account (ideally as a restricted, `command=`-forced,
  read-only authorized key).
- `peer_known_hosts` — the peer host key(s). Left empty, the flow uses
  `StrictHostKeyChecking=accept-new` and appends the key on first connect; pre-pin
  it for stricter security.

Then set `PEER_HOST` (peer host/IP) via `./config.sh` (or `.env`), re-render
config, rebuild the worker (`openssh-client` + `rsync` are baked into
`prefect/Dockerfile`), and re-register the deployment:
`docker compose exec prefect-worker sh -c "cd /flows && prefect deploy --all"`.

Peer-side prerequisites: an SSH account reachable on `PEER_SSH_PORT`, allowed
through the firewall from this site's IP, able to (a) `rsync`-read
`/var/lib/archive` and `/var/lib/workspace`, and (b) run `PEER_PSQL`
(default `psql -d internal`) read-only against its own `internal` database
(the exchange SQL is piped to it on stdin).
