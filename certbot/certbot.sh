#!/bin/bash

trap exit TERM

certbot certonly \
  --webroot \
  --webroot-path=/var/www/certbot \
  --email bmrbsys@protein.osaka-u.ac.jp \
  --agree-tos \
  --no-eff-email \
  -d bmrb-extract.pdbj.org

while :; do
  certbot renew
  sleep 12h
done

