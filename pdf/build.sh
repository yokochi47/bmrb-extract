#!/bin/bash
#
# Build the local PDF-report generator image (bmrb-extract-pdf-report:local).
#
# Stages the shared chart builders (frontend/src/app/pages/report-charts.ts) into
# this build context so the image can bundle them with esbuild, then builds the
# image. Run from anywhere; paths are resolved relative to this script.
#
set -eu

here="$(cd "$(dirname "$0")" && pwd)"
cd "$here"

image="${PDF_REPORT_IMAGE:-bmrb-extract-pdf-report:local}"
src="../frontend/src/app/pages/report-charts.ts"

if [[ ! -f "$src" ]]; then
  echo "error: shared chart module not found at $src" >&2
  exit 1
fi

# Stage the shared chart module into the build context (Docker cannot COPY from
# outside its context). Regenerated on every build; kept out of git.
mkdir -p src
cp "$src" src/report-charts.ts

# Stage the service icon (used on the title page and page footer) — a PNG so it
# rasterizes into the PDF without any SVG-feature limitations.
mkdir -p assets
cp report_logo.png assets/report_logo.png

echo "Building $image ..."
docker build -t "$image" .
echo "Built $image"
