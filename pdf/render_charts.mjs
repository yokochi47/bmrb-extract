/**
 * Server-side ECharts renderer for the static PDF conversion report.
 *
 * Reads a chart-inputs JSON produced by the Python generator, builds each chart
 * with the SHARED option builders (frontend/src/app/pages/report-charts.ts,
 * bundled to vendor/report-charts.mjs by `npm run bundle-charts`) and renders it
 * to a standalone SVG via ECharts' server-side SVG renderer. This keeps chart
 * appearance identical to the on-screen report without re-implementing it here.
 *
 * Usage:  node render_charts.mjs <chart_inputs.json> <out_dir>
 *
 * chart_inputs.json is an array of chart specs; Python owns all data-shaping:
 *   [
 *     { "id": "dist_violation", "builder": "distViolationChart",
 *       "args": [ <rows> ], "width": 680, "height": 420 },
 *     { "id": "ens_pca", "builder": "pcaChartOption", "args": [ <clusters> ] },
 *     ...
 *   ]
 * `builder` is an exported function name from report-charts; `args` is the exact
 * positional argument list that function expects. A builder that returns null
 * (no data) yields a null entry — no SVG file is written for it.
 *
 * Writes <out_dir>/<id>.svg for each chart plus <out_dir>/charts.json mapping
 * id -> svg filename (or null). The Python side embeds the SVGs by id.
 */
import fs from 'node:fs';
import path from 'node:path';
import * as echarts from 'echarts';
import * as builders from './vendor/report-charts.mjs';

const DEFAULT_WIDTH = 680;
const DEFAULT_HEIGHT = 420;

function main() {
  const [, , inputsPath, outDir] = process.argv;
  if (!inputsPath || !outDir) {
    console.error('usage: node render_charts.mjs <chart_inputs.json> <out_dir>');
    process.exit(2);
  }

  const specs = JSON.parse(fs.readFileSync(inputsPath, 'utf8'));
  if (!Array.isArray(specs)) {
    console.error('chart_inputs.json must be an array of chart specs');
    process.exit(2);
  }
  fs.mkdirSync(outDir, { recursive: true });

  const manifest = {};
  for (const spec of specs) {
    const { id, builder, args = [] } = spec ?? {};
    if (!id || !builder) {
      console.error(`[render_charts] skipping spec with missing id/builder: ${JSON.stringify(spec)}`);
      continue;
    }
    const fn = builders[builder];
    if (typeof fn !== 'function') {
      console.error(`[render_charts] unknown builder '${builder}' for chart '${id}'`);
      manifest[id] = null;
      continue;
    }

    let built;
    try {
      built = fn(...args);
    } catch (err) {
      console.error(`[render_charts] builder '${builder}' failed for '${id}': ${err?.stack || err}`);
      manifest[id] = null;
      continue;
    }
    if (built == null) {
      // Builder returned null (no data for this chart) — nothing to render.
      manifest[id] = null;
      continue;
    }

    // pcaChartOption wraps its option as { option, marginX, marginY }; every
    // other builder returns the option object directly.
    const option = built.option ?? built;
    // SSR must disable animation, otherwise ECharts schedules timers that keep
    // the Node event loop alive and the process never exits.
    option.animation = false;

    const width = Number(spec.width) || DEFAULT_WIDTH;
    const height = Number(spec.height) || DEFAULT_HEIGHT;
    const chart = echarts.init(null, null, { renderer: 'svg', ssr: true, width, height });
    chart.setOption(option);
    const svg = chart.renderToSVGString();
    chart.dispose();

    const file = `${id}.svg`;
    fs.writeFileSync(path.join(outDir, file), svg, 'utf8');
    manifest[id] = file;
  }

  fs.writeFileSync(path.join(outDir, 'charts.json'), JSON.stringify(manifest, null, 2), 'utf8');
  console.error(`[render_charts] wrote ${Object.values(manifest).filter(Boolean).length} SVG(s) to ${outDir}`);
  // Force exit: ECharts may leave timers pending even after dispose().
  process.exit(0);
}

main();
