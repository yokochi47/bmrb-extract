# =========================
# Build stage
# =========================
FROM alpine:latest AS build

ARG NGINX_VERSION=1.30.4

RUN apk add --no-cache \
    build-base \
    linux-headers \
    cmake \
    curl \
    git \
    perl \
    zlib-dev \
    pcre2-dev \
    brotli-dev \
    openssl \
    openssl-dev

WORKDIR /build

ENV MIN_NGINX_VERSION=1.25.3
ENV MIN_OPENSSL_VERSION=3.5.1

# ---- Check OpenSSL version (QUIC requires >= 3.5)
RUN OPENSSL_VERSION=`openssl version`; \
    if [ "$(printf '%s\n' "$MIN_OPENSSL_VERSION" "$OPENSSL_VERSION" | sort -V | head -n1)" = "$MIN_OPENSSL_VERSION" ]; then \
     echo "Version OK"; else exit 1; fi

# ---- Download Nginx ----
RUN echo "Nginx version: $NGINX_VERSION"; \
 if [ "$(printf '%s\n' "$MIN_NGINX_VERSION" "$NGINX_VERSION" | sort -V | head -n1)" = "$MIN_NGINX_VERSION" ]; then \
    echo "Version OK"; else exit 1; fi; \
 curl -LO https://nginx.org/download/nginx-${NGINX_VERSION}.tar.gz \
 && tar xzf nginx-${NGINX_VERSION}.tar.gz

WORKDIR /build/nginx-${NGINX_VERSION}

# ---- Configure Nginx with QUIC ----
RUN ./configure \
    --prefix=/etc/nginx \
    --sbin-path=/usr/sbin/nginx \
    --conf-path=/etc/nginx/nginx.conf \
    --error-log-path=/var/log/nginx/error.log \
    --http-log-path=/var/log/nginx/access.log \
    --pid-path=/var/run/nginx.pid \
    --with-http_ssl_module \
    --with-http_v2_module \
    --with-http_v3_module \
    --with-pcre-jit \
    --with-threads \
    --with-openssl-opt=enable-tls1_3 \
    --with-stream \
    --with-stream_ssl_module \
    --with-cc-opt="-O2" \
    --with-ld-opt="-lstdc++" \
 && make -j$(nproc) \
 && make install

# =========================
# Runtime stage
# =========================
FROM alpine:latest

RUN apk add --no-cache \
    libstdc++ \
    pcre2 \
    zlib \
    brotli \
    ca-certificates \
    tzdata \
    logrotate \
    busybox-suid \
    openssl

# Nginx runtime user
RUN addgroup -S nginx && adduser -S nginx -G nginx

ARG CACHEBUST=1

COPY --from=build /usr/sbin/nginx /usr/sbin/nginx
COPY --from=build /etc/nginx /etc/nginx
COPY --from=bmrb-extract-frontend:latest /data /usr/share/nginx/html

COPY logrotate-nginx.conf /etc/logrotate.d/nginx

RUN mkdir -p /var/log/nginx /var/www/html \
 && chown -R nginx:nginx /var/log/nginx

EXPOSE 80/tcp
EXPOSE 443/tcp
EXPOSE 443/udp

STOPSIGNAL SIGQUIT

COPY entrypoint.sh /opt/entrypoint.sh
RUN chmod +x /opt/entrypoint.sh

ENTRYPOINT ["/opt/entrypoint.sh"]

