#!/bin/sh
set -e

# Create vhost log directories if missing
mkdir -p /var/log/nginx/${SERVICE_HOST}
chown -R nginx:nginx /var/log/nginx

# Run logrotate daily via cron
echo "0 0 * * * /usr/sbin/logrotate /etc/logrotate.d/nginx" > /etc/crontabs/root
crond

# Graceful shutdown
trap "nginx -s quit; exit 0" TERM INT

# Start nginx
nginx

# Zero-downtime reload loop (optional hook)
while true; do
    sleep 6h
    nginx -s reload
done

# Start nginx in foreground
# exec nginx -g "daemon off;"

