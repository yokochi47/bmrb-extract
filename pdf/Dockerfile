# PDF-report generator: Python 3 + WeasyPrint (document layout / paged media) and
# Node + ECharts (server-side SVG chart rendering). Invoked by the prefect
# convert_pdf task via `docker run` to turn the last NMR data-processing JSON
# report into C_<id>_report.pdf.
FROM node:20-bookworm-slim

# WeasyPrint runtime libs (pango/cairo/gdk-pixbuf/harfbuzz), the Lato font used by
# the web report, and Python 3. fonts-lato matches the on-screen report typeface.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip python3-venv \
      libpango-1.0-0 libpangocairo-1.0-0 libpangoft2-1.0-0 \
      libgdk-pixbuf-2.0-0 libcairo2 libffi8 \
      fonts-lato fontconfig \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -f

# Python deps (PEP 668: this is a dedicated image, install into the system env).
RUN pip3 install --no-cache-dir --break-system-packages weasyprint==63.1 jinja2==3.1.4

WORKDIR /app

# Node deps + bundle the shared chart builders (src/report-charts.ts is staged
# into the build context by build.sh) into vendor/report-charts.mjs.
COPY package.json package-lock.json ./
RUN npm ci
COPY src/report-charts.ts ./src/report-charts.ts
RUN npm run bundle-charts

# Generator sources and baked-in assets (service icon, fonts handled by fontconfig).
COPY render_charts.mjs generate_report.py ./
COPY templates/ ./templates/
COPY assets/ ./assets/

# The flow passes explicit file paths; there is no default command.
ENTRYPOINT ["python3", "generate_report.py"]
