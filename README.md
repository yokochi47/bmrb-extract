<p align="center">
  <a href="https://bmrb-extract.pdbj.org"><img src="https://raw.githubusercontent.com/yokochi47/bmrb-extract/refs/heads/main/frontend/public/bmrb_extract_logo.svg" width="300" height="140" alt="bmrb_extract"/></a>
</p>

**bmrb_extract** is an NMR data conversion service that simplifies data deposition to PDB (via OneDep) and BMRB (via BMRBdep) by creating a single NMR data file from various software-native formats. The service is designed to be hosted by [BMRB](https://bmrb.io) and [BMRBj](https://bmrbj.pdbj.org).

## Requirements
- Docker Engine
- Node.js

## Install
1. Configure service (generates .env, nginx.conf, init.sql, site_config.py, etc.)
```shell
./config.sh
```
2. Setup service (build Docker images, create volumes, init GitHub Action runners, start Docker Swarm services, build Docker images)
```shell
./setup.sh  # use '--no-cache' for clean build
```
3. Start/Stop service
```shell
./start.sh
./stop.sh
```
To rebuild web frontend + nginx only (for frontend development)
```shell
./reload_wfe.sh
```

4. Run at boot / stop at shutdown (optional, one-time; needs sudo)
```shell
./install_systemd.sh
```
This installs and enables `bmrb-extract.service`, which runs `./setup.sh && ./start.sh` at boot and
`./stop.sh` at shutdown. It also installs a narrowly scoped `/etc/sudoers.d/bmrb-extract` (setup.sh
calls `sudo` unconditionally and systemd gives it no TTY) and `/etc/sysctl.d/99-bmrb-extract.conf`
(persists the HTTP/3 and Redis kernel tunables setup.sh would otherwise set with `sudo sysctl -w`
on every boot).

Once enabled, prefer these over `./start.sh` / `./stop.sh`:
```shell
sudo systemctl start bmrb-extract
sudo systemctl stop bmrb-extract
systemctl status bmrb-extract
journalctl -u bmrb-extract -f
```

Things to know:
- **Boot is not instant.** `setup.sh` re-runs `docker compose build` and `./pdf/build.sh` every time
  — roughly 2-15 min with a warm cache. When upstream publishes a new nginx release, `NGINX_VERSION`
  in `.env` changes and nginx is recompiled from source (20-40 min). The site is down until the unit
  finishes; `TimeoutStartSec=infinity` is what stops systemd killing it mid-build.
- **The unit needs the network before it starts.** Sourcing `.env` curls github.com to resolve
  `NGINX_VERSION`; if that fails the value is empty and the nginx build's version guard rejects it.
  Pin `NGINX_VERSION` to a literal in `.env` to remove the dependency.
- **Shutdown now removes the containers** (`docker compose down`), so `restart: unless-stopped` no
  longer brings them back — the unit is the only thing that starts the stack. If it fails, the site
  stays down; check `systemctl status bmrb-extract`.
- `systemctl restart docker` stops this unit without restarting it (`Requires=docker.service`).
  Follow it with `sudo systemctl restart bmrb-extract`.
