<p align="center">
  <a href="https://bmrb-extract.pdbj.org"><img src="https://raw.githubusercontent.com/yokochi47/bmrb-extract/refs/heads/main/frontend/public/bmrb_extract_logo.svg" width="300" height="140" alt="bmrb_extract"/></a>
</p>

**bmrb_extract** is an NMR data conversion service that simplifies data deposition to PDB (via OneDep) and BMRB (via BMRBdep) by creating a single NMR data file from various software-native formats. The service is designed to be hosted by [BMRB](https://bmrb.io) and [BMRBj](https://bmrbj.pdbj.org).

## Requirements
- Docker engin
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
