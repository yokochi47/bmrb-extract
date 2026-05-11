# bmrb_extract

**bmrb_extract** is an NMR data conversion service that simplifies data deposition to PDB (via OneDep) and BMRB (via BMRBdep) by creating a single NMR data file from various software-native formats. The service is designed to be hosted by [BMRB](https://bmrb.io) and [BMRBj](https://bmrbj.pdbj.org).

## Requirements
- Docker engin

## Install
1. Configure service
```shell
./config.sh
```
2. Setup
```shell
./setup.sh
```
3. Start/Stop service
```shell
./start.sh
./stop.sh
```
To reload the web frontend (for developers)
```shell
./reload_wfe.sh
```
