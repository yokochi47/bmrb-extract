# Flask/Gunicorn API for the local deployment.
#
# Differs from backend/Dockerfile in two ways: the build context is the repo
# root (so the shared upstream templates can be copied in), and the application
# source is BAKED IN rather than bind-mounted, so the running service is exactly
# what was built. core/site_config.py is rendered by the entrypoint from the
# environment on every start.

FROM python:3.12-alpine

ENV PIP_ROOT_USER_ACTION=ignore \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# gcc/musl-dev/libffi-dev build the wheels that have no musl binaries;
# git backs the per-session upload archive (GitPython);
# gettext provides envsubst for the site-config renderer.
RUN apk add --no-cache gcc musl-dev libffi-dev git gettext

WORKDIR /app

COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application source. .dockerignore keeps any locally-rendered site_config.py
# out of the context so it cannot shadow the one the entrypoint writes.
COPY backend/app/ /app/

# Upstream's site-config template, rendered at start rather than at build.
COPY backend/app/core/site_config.py.template /opt/bmrb-extract/site_config.py.template

COPY docker/common/render-site-config.sh /usr/local/bin/render-site-config
COPY docker/backend/entrypoint.sh /usr/local/bin/entrypoint
RUN chmod +x /usr/local/bin/render-site-config /usr/local/bin/entrypoint

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/entrypoint"]
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "app:app"]
