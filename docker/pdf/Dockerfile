# PDF-report generator: Python 3 + WeasyPrint (paged layout) and Node + ECharts
# (server-side SVG charts). The convert_pdf task runs it via `docker run`.
#
# Identical output to pdf/Dockerfile, but the build context is the repo root, so
# the shared chart module and the logo are copied from their canonical locations
# instead of being staged into pdf/ by pdf/build.sh first. That removes the last
# host-side build step.

FROM node:20-bookworm-slim

# WeasyPrint runtime libs (pango/cairo/gdk-pixbuf), the Lato font the on-screen
# report uses, and Python 3.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip python3-venv \
      libpango-1.0-0 libpangocairo-1.0-0 libpangoft2-1.0-0 \
      libgdk-pixbuf-2.0-0 libcairo2 libffi8 \
      fonts-lato fontconfig \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -f

# PEP 668: dedicated image, so installing into the system environment is fine.
RUN pip3 install --no-cache-dir --break-system-packages weasyprint==63.1 jinja2==3.1.4

WORKDIR /app

COPY pdf/package.json pdf/package-lock.json ./
RUN npm ci

# The charts in the PDF are the SAME code as the on-screen ones. Copied straight
# from the frontend source — keep report-charts.ts free of Angular and DOM.
COPY frontend/src/app/pages/report-charts.ts ./src/report-charts.ts
RUN npm run bundle-charts

COPY pdf/render_charts.mjs pdf/generate_report.py pdf/report_data.py ./
COPY pdf/templates/ ./templates/
COPY pdf/report_logo.png ./assets/report_logo.png

# The flow passes explicit file paths; there is no default command.
ENTRYPOINT ["python3", "generate_report.py"]
