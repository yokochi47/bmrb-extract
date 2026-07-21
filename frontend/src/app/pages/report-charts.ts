/**
 * Framework-agnostic ECharts option builders for the data-conversion statistical
 * report. These pure functions are the single source of truth for chart
 * appearance: the Angular download page imports them, and the server-side PDF
 * renderer (Node + ECharts SSR) imports the very same functions so the static
 * PDF charts match the on-screen ones. Keep this module free of Angular (and any
 * browser/DOM) dependencies so it stays importable from a plain Node runtime.
 */

/* ------------------------------------------------------------------ types --- */

/** One cluster of the coordinate ensemble (cluster_id === -1 → single-model). */
export interface PcaCluster {
  cluster_id?: number;
  /** Per-model PC1/PC2 coordinates for the PCA scatter. */
  principal_components?: { model_id?: number; pc1?: number; pc2?: number }[];
}

/** A stacked/annotated histogram (chem-shift Z-scores). */
export interface HistogramChart {
  label: string;
  categories: string[];
  series: { name: string; data: number[] }[];
  /** Outlier markers: dashed line + short description. `x` is the fractional
   * category-axis index of the value. */
  annotations?: { x: number; anomalous: boolean; text: string }[];
}

/** A per-residue line chart (RCI/S² or NMR RMSD) with structural bands. */
export interface PerResidueLine {
  categories: string[];
  series: { name: string; data: (number | null)[] }[];
  bands: { start: number; end: number; type: string; label: string }[];
  ymin: number | null;
  ymax: number | null;
  threshold: number | null;
}

/** One category row of {dist,dihed}_violation_summary (fields used by the bars). */
export interface ViolationSummaryRow {
  restraint_type?: string;
  restraint_count?: number;
  viol_count?: number;
  consist_viol_count?: number;
}

/** One most-violated restraint (fields used by the mean-violation histogram). */
export interface MeanViolationRow {
  distance_type?: string;
  dihedral_angle_name?: string;
  mean_violation?: number | null;
}

/** A category/color mapping for the stacked violation charts. */
export interface ViolationCategory {
  key: string;
  label: string;
  color?: string;
}

/* -------------------------------------------------------------- constants --- */

/** Preferred category order for the distance mean-violation histogram. */
export const DIST_CAT_ORDER = [
  'intra-residue',
  'sequential',
  'medium_range',
  'long_range',
  'inter-chain',
  'hydrogen_bond',
  'disulfide_bond',
  'diselenide_bond',
  'metal_coordiantion',
];

/** Hatch decal (diagonal lines) for the "Violated" overlay bars. */
const VIOL_DECAL = {
  color: 'rgba(0, 0, 0, 0.55)',
  dashArrayX: [1, 0],
  dashArrayY: [2, 4],
  rotation: -Math.PI / 4,
};

/** Color for the per-model mean/median markers and mean±SD error bars. */
const MARK_COLOR = '#000000';
/** Plus glyph in a 10×10 box; drawn with symbolRotate:45 to render as an "×"
 * (shared by the chart symbol and the tooltip marker so they match). */
const MEDIAN_PATH = 'M3,0 L7,0 L7,3 L10,3 L10,7 L7,7 L7,10 L3,10 L3,7 L0,7 L0,3 L3,3 Z';
const MEDIAN_SYMBOL = `path://${MEDIAN_PATH}`;

const LEGEND_CAP = 160;

/* ---------------------------------------------------------------- helpers --- */

/** Reserved right-margin width (px) for a vertical legend, capped. */
function legendReserve(names: string[]): number {
  const maxLen = names.reduce((m, n) => Math.max(m, n.length), 0);
  return Math.min(LEGEND_CAP, Math.round(82 + 5.8 * maxLen));
}

/** Display label for a violation-summary restraint_type: underscores become
 * spaces; a leading abbreviation prefix ("ir;", "lr;", "total;", …) becomes a
 * two-space (non-breaking) indent and stays lower-case; top-level types have
 * their first character capitalized. */
export function restraintTypeLabel(type: string | undefined): string {
  if (!type) return '';
  const semi = type.indexOf(';');
  if (semi >= 0) {
    // Two non-breaking spaces indent the sub-type under its abbreviation prefix.
    return (
      '  ' +
      type
        .slice(semi + 1)
        .replace(/_/g, ' ')
        .trimStart()
    );
  }
  const s = type.replace(/_/g, ' ').toLowerCase();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/** A "nice" bin width for a 0..max range targeting ~30 bins (1/2/5 × 10ⁿ). */
function niceStep(max: number): number {
  const target = max > 0 ? max / 30 : 1;
  const pow = Math.pow(10, Math.floor(Math.log10(target)));
  const norm = target / pow;
  const nice = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10;
  return nice * pow;
}

/** Structural-band fill color by type (secondary structure + ensemble domain). */
function bandColor(type: string): string {
  if (type === 'helix') return 'rgba(204,47,0,0.12)';
  if (type === 'strand') return 'rgba(0,156,209,0.12)';
  if (type === 'turn') return 'rgba(200,204,0,0.18)';
  if (type === 'core') return 'rgba(224,255,255,0.6)'; // well-defined core: lightcyan
  if (type === 'unmodeled') return 'rgba(211,211,211,0.55)'; // unmodeled residues: lightgray
  return 'rgba(120,120,120,0.08)';
}
/** More saturated band color used as a thin edge line on the band's sides. */
function bandEdgeColor(type: string): string {
  if (type === 'helix') return 'rgba(204,47,0,0.55)';
  if (type === 'strand') return 'rgba(0,156,209,0.55)';
  if (type === 'turn') return 'rgba(200,204,0,0.65)';
  if (type === 'core') return 'rgba(0,181,204,0.6)';
  if (type === 'unmodeled') return 'rgba(150,150,150,0.6)';
  return 'rgba(120,120,120,0.4)';
}

/** Structural bands as a markArea overlay anchored to a hidden value axis
 * (xAxisIndex 1) whose value v maps to category fraction v/n, so [start, end+1]
 * covers the full bins of the band's first/last residue. */
function bandOverlay(
  categories: string[],
  bands: { start: number; end: number; type: string; label: string }[],
): { markerAxis: object; holderSeries: object } {
  const markArea = {
    silent: true,
    label: {
      show: true,
      position: 'insideTopLeft',
      rotate: -90,
      fontSize: 10,
      color: '#64748b',
      distance: 11,
    },
    data: bands.map((b) => [
      {
        xAxis: b.start,
        itemStyle: {
          color: bandColor(b.type),
          borderColor: bandEdgeColor(b.type),
          borderWidth: 1,
        },
        name: b.label,
      },
      { xAxis: b.end + 1 },
    ]),
  };
  const markerAxis = {
    type: 'value',
    min: 0,
    max: categories.length,
    show: false,
    axisPointer: { show: false },
  };
  return {
    markerAxis,
    holderSeries: { type: 'line', xAxisIndex: 1, data: [], silent: true, markArea },
  };
}

/** renderItem for the mean±SD error bars (a custom series): a vertical I-beam
 * on the right (violation-magnitude) axis. */
const errorBarRenderItem = (
  _params: unknown,
  api: { value(i: number): number; coord(p: number[]): number[] },
): object => {
  const low = api.coord([api.value(0), api.value(1)]);
  const high = api.coord([api.value(0), api.value(2)]);
  const x = low[0];
  const w = 4;
  const style = { stroke: MARK_COLOR, lineWidth: 1 };
  return {
    type: 'group',
    children: [
      { type: 'line', shape: { x1: x, y1: low[1], x2: x, y2: high[1] }, style },
      { type: 'line', shape: { x1: x - w, y1: low[1], x2: x + w, y2: low[1] }, style },
      { type: 'line', shape: { x1: x - w, y1: high[1], x2: x + w, y2: high[1] }, style },
    ],
  };
};

/* --------------------------------------------------------------- builders --- */

/** PCA scatter (PC1 vs PC2) — one series per cluster (cluster_id === -1 →
 * 'Single-model'); each point carries its cluster and model in the tooltip.
 * Null when no cluster reports principal components. */
export function pcaChartOption(
  clusters: PcaCluster[],
): { option: object; marginX: number; marginY: number } | null {
  const series = clusters
    .map((c) => {
      const pcs = c.principal_components ?? [];
      if (!pcs.length) return null;
      const name = c.cluster_id === -1 ? 'Single-model' : `Cluster ${c.cluster_id}`;
      return {
        name,
        type: 'scatter',
        symbolSize: 10,
        data: pcs.map((p) => ({ value: [p.pc1, p.pc2, p.model_id] })),
      };
    })
    .filter((s): s is NonNullable<typeof s> => s !== null);
  if (!series.length) return null;
  const names = series.map((s) => s.name);
  const legendW = legendReserve(names);
  return {
    // Square plot area: marginX reserves the left axis + right-side legend,
    // marginY the top padding + bottom x-axis title.
    marginX: 48 + legendW,
    marginY: 56,
    option: {
      tooltip: {
        trigger: 'item',
        formatter: (p: { seriesName?: string; value?: number[] }) => {
          const v = p.value ?? [];
          return `${p.seriesName} · Model ${v[2]}<br/>PC1: ${(+v[0]).toFixed(3)}, PC2: ${(+v[1]).toFixed(3)}`;
        },
      },
      legend: { orient: 'vertical', right: 8, top: 'middle', type: 'plain', data: names },
      grid: { left: 48, right: legendW, top: 16, bottom: 40, containLabel: true },
      xAxis: { type: 'value', name: 'PC1', nameLocation: 'middle', nameGap: 26, scale: true },
      yAxis: { type: 'value', name: 'PC2', nameLocation: 'middle', nameGap: 40, scale: true },
      series,
    },
  };
}

/** ECharts option for a normalized chemical-shift histogram. Bars are stacked
 * per isotope; optional Z-score outlier markers are drawn as dashed markLines
 * against a hidden value axis. */
export function histogramOption(
  h: HistogramChart,
  xName: string,
  yName: string,
  opts: { inverse?: boolean; rangeLabels?: boolean; yAxisLine?: boolean } = {},
): object {
  const { inverse = false, rangeLabels = false, yAxisLine = false } = opts;
  const step =
    h.categories.length >= 2 ? parseFloat(h.categories[1]) - parseFloat(h.categories[0]) : 0;
  const labelFormatter =
    rangeLabels && step && inverse
      ? (value: string) => `(${+(parseFloat(value) + step).toFixed(6)}, ${value}]`
      : rangeLabels && step
        ? (value: string) => `[${value}, ${+(parseFloat(value) + step).toFixed(6)})`
        : undefined;
  const ann = h.annotations ?? [];
  const n = h.categories.length;
  const markLine = ann.length
    ? {
        silent: true,
        symbol: 'none',
        data: ann.map((a) => ({
          xAxis: a.x,
          lineStyle: { color: a.anomalous ? '#dc2626' : '#475569', type: 'dashed', width: 1 },
          label: {
            show: true,
            formatter: a.text,
            position: 'end',
            rotate: -90,
            align: 'left',
            verticalAlign: 'bottom',
            fontSize: 9,
            color: a.anomalous ? '#dc2626' : '#475569',
          },
        })),
      }
    : undefined;
  const categoryAxis = {
    type: 'category',
    data: h.categories,
    name: xName,
    nameLocation: 'middle',
    nameGap: 40,
    axisLabel: { rotate: -75, fontSize: 9, formatter: labelFormatter },
    inverse,
  };
  const markerAxis = {
    type: 'value',
    min: 0.0,
    max: n + 1.0,
    show: false,
    axisPointer: { show: false },
  };
  return {
    title: { text: h.label, left: 'center', textStyle: { fontSize: 12, fontWeight: 'normal' } },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    legend: { bottom: 0, type: 'scroll', data: h.series.map((s) => s.name) },
    grid: { left: 56, right: 16, top: 36, bottom: 64, containLabel: true },
    xAxis: markLine ? [categoryAxis, markerAxis] : categoryAxis,
    yAxis: {
      type: 'value',
      name: yName,
      minInterval: 1,
      axisTick: { show: true },
      ...(yAxisLine ? { axisLine: { show: true } } : {}),
    },
    series: [
      ...h.series.map((s) => ({ name: s.name, type: 'bar', stack: 'total', data: s.data })),
      ...(markLine ? [{ type: 'line', xAxisIndex: 1, data: [], silent: true, markLine }] : []),
    ],
  };
}

/** ECharts option for a per-residue line chart (RCI/S² or NMR RMSD) with
 * structural bands and an optional well-defined-region threshold line. */
export function lineOption(c: PerResidueLine): object {
  const interval = Math.max(0, Math.ceil(c.categories.length / 24) - 1);
  const { markerAxis, holderSeries } = bandOverlay(c.categories, c.bands);
  const markLine =
    c.threshold !== null
      ? {
          silent: true,
          symbol: 'none',
          label: {
            position: 'insideStartTop',
            fontSize: 10,
            formatter: `RMSD in well-defined region of the coordinates: ${c.threshold}Å`,
            distance: 0,
          },
          data: [{ yAxis: c.threshold }],
          lineStyle: { color: '#64748b', type: 'dashed' },
        }
      : undefined;
  return {
    tooltip: { trigger: 'axis' },
    legend: { bottom: 0, type: 'scroll', data: c.series.map((s) => s.name) },
    grid: { left: 52, right: 16, top: 24, bottom: 64, containLabel: true },
    xAxis: [
      { type: 'category', data: c.categories, axisLabel: { interval, rotate: -75, fontSize: 8 } },
      markerAxis,
    ],
    yAxis: {
      type: 'value',
      axisLine: { show: true },
      ...(c.ymin !== null ? { min: c.ymin } : {}),
      ...(c.ymax !== null ? { max: c.ymax } : {}),
    },
    series: [
      ...c.series.map((s, idx) => ({
        name: s.name,
        type: 'line',
        data: s.data,
        connectNulls: false,
        showSymbol: true,
        symbolSize: 4,
        ...(idx === 0 && markLine ? { markLine } : {}),
      })),
      holderSeries,
    ],
  };
}

/** Stacked bar chart of violated restraints vs ensemble fraction (%), stacked
 * by category. cats without an explicit color use the default palette. */
export function violationEnsembleStackChart(
  rows: Record<string, number | null>[],
  cats: ViolationCategory[],
): object | null {
  if (!rows.length || !cats.length) return null;
  const num = (v: number | null | undefined): number => (typeof v === 'number' ? v : 0);
  const xLabels = rows.map((r) => String(num(r['fraction_percent'])));
  return {
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (
        params: { axisValue?: string; seriesName?: string; marker?: string; value?: number }[],
      ) => {
        const header = `${params[0]?.axisValue ?? ''}% of the ensemble`;
        const lines = params
          .filter((p) => p.value)
          .map((p) => `${p.marker ?? ''}${p.seriesName}: ${p.value}`);
        return [header, ...lines].join('<br/>');
      },
    },
    legend: {
      orient: 'vertical',
      right: 8,
      top: 'middle',
      type: 'scroll',
      data: cats.map((c) => c.label),
    },
    grid: { left: 60, right: 170, top: 24, bottom: 56, containLabel: true },
    xAxis: {
      type: 'category',
      data: xLabels,
      name: 'Fraction of the ensemble (%)',
      nameLocation: 'middle',
      nameGap: 32,
      axisLabel: { fontSize: 9 },
    },
    yAxis: {
      type: 'value',
      name: 'Number of violated restraints',
      nameLocation: 'middle',
      nameGap: 44,
    },
    series: cats.map((c) => ({
      name: c.label,
      type: 'bar',
      stack: 'v',
      data: rows.map((r) => num(r[c.key])),
      ...(c.color ? { itemStyle: { color: c.color } } : {}),
    })),
  };
}

/** Stacked histogram of {value} counts, binned on the x-axis (from 0) and
 * stacked by category. Null when there are no data. */
export function stackedValueHistogram(
  pts: { value: number; cat: string }[],
  unit: string,
  order: string[],
  xName: string,
): object | null {
  if (!pts.length) return null;
  const maxV = Math.max(...pts.map((p) => p.value));
  const step = niceStep(maxV);
  const nBins = Math.max(1, Math.ceil((maxV + 1e-9) / step));
  const decimals = step < 1 ? 2 : 0;
  const binLabels = Array.from({ length: nBins }, (_, i) => (i * step).toFixed(decimals));
  const orderIndex = (c: string) => {
    const i = order.indexOf(c.toLowerCase());
    return i === -1 ? order.length : i;
  };
  const cats = [...new Set(pts.map((p) => p.cat))].sort(
    (a, b) => orderIndex(a) - orderIndex(b) || a.localeCompare(b),
  );
  const series = cats.map((cat) => {
    const data = new Array(nBins).fill(0);
    for (const p of pts) {
      if (p.cat === cat) data[Math.min(nBins - 1, Math.floor(p.value / step))]++;
    }
    return { name: restraintTypeLabel(cat), type: 'bar', stack: 'v', data };
  });
  return {
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (
        params: { axisValue?: string; seriesName?: string; marker?: string; value?: number }[],
      ) => {
        const lo = Number(params[0]?.axisValue ?? 0);
        const header = `[${lo.toFixed(decimals)}, ${(lo + step).toFixed(decimals)}) ${unit}`;
        const lines = params
          .filter((p) => p.value)
          .map((p) => `${p.marker ?? ''}${p.seriesName}: ${p.value}`);
        return [header, ...lines].join('<br/>');
      },
    },
    legend: {
      orient: 'vertical',
      right: 8,
      top: 'middle',
      type: 'scroll',
      data: cats.map((c) => restraintTypeLabel(c)),
    },
    grid: { left: 60, right: 170, top: 24, bottom: 56, containLabel: true },
    xAxis: {
      type: 'category',
      data: binLabels,
      name: `${xName} (${unit})`,
      nameLocation: 'middle',
      nameGap: 32,
      axisLabel: { fontSize: 9 },
    },
    yAxis: { type: 'value', name: 'Count', nameLocation: 'middle', nameGap: 40, minInterval: 1 },
    series,
  };
}

/** Stacked histogram of per-restraint mean violations, by restraint category. */
export function meanViolationHistogram(
  rows: MeanViolationRow[],
  catKey: 'distance_type' | 'dihedral_angle_name',
  unit: string,
  order: string[],
): object | null {
  const pts = rows
    .map((r) => ({
      value: typeof r.mean_violation === 'number' ? r.mean_violation : null,
      cat: String(r[catKey] ?? ''),
    }))
    .filter((p): p is { value: number; cat: string } => p.value !== null && p.cat !== '');
  return stackedValueHistogram(pts, unit, order, 'Mean violation');
}

/** Grouped bar chart of distance restraints (by sub-type) with the violated
 * (hatched) and consistently-violated (solid black) counts overlaid on each
 * restraint bar. Null when there are no distance restraints. */
export function distViolationChart(rows: ViolationSummaryRow[]): object | null {
  if (!rows.length) return null;
  const cats = [
    { abbr: 'ir', label: 'Intra-residue' },
    { abbr: 'sq', label: 'Sequential' },
    { abbr: 'mr', label: 'Medium range' },
    { abbr: 'lr', label: 'Long range' },
    { abbr: 'ic', label: 'Inter-chain' },
  ];
  const subs = [
    { key: 'backbone-backbone', label: 'Backbone-Backbone', color: '#5470c6' },
    { key: 'backbone-sidechain', label: 'Backbone-Sidechain', color: '#91cc75' },
    { key: 'sidechain-sidechain', label: 'Sidechain-Sidechain', color: '#fac858' },
  ];
  // restraint_type is "<abbr>; <sub-type>"; normalise whitespace to match.
  const byType = new Map(rows.map((r) => [(r.restraint_type ?? '').replace(/\s+/g, ''), r] as const));
  const xLabels: string[] = [];
  // Category label for every bar (the axis label is blanked except under the
  // middle sub-type bar), used to show the category in the tooltip.
  const fullLabels: string[] = [];
  const restraint = subs.map(() => [] as (number | null)[]);
  const violated: number[] = [];
  const consistent: number[] = [];
  for (const c of cats) {
    subs.forEach((s, j) => {
      const r = byType.get(`${c.abbr};${s.key}`);
      // Label the category once, under its middle sub-type bar.
      xLabels.push(j === 1 ? c.label : '');
      fullLabels.push(c.label);
      restraint.forEach((d, k) => d.push(k === j ? (r?.restraint_count ?? 0) : null));
      violated.push(r?.viol_count ?? 0);
      consistent.push(r?.consist_viol_count ?? 0);
    });
  }
  return {
    tooltip: {
      trigger: 'item',
      formatter: (p: { dataIndex: number; seriesName?: string; value?: number | null }) =>
        `${fullLabels[p.dataIndex]} — ${p.seriesName}<br/>${p.value ?? 0}`,
    },
    legend: {
      orient: 'vertical',
      right: 8,
      top: 'middle',
      type: 'scroll',
      data: [...subs.map((s) => s.label), 'Violated', 'Consistently violated'],
    },
    grid: { left: 56, right: 170, top: 24, bottom: 40, containLabel: true },
    xAxis: { type: 'category', data: xLabels, axisLabel: { rotate: -30, fontSize: 10 } },
    yAxis: { type: 'value', name: 'Number of restraints', nameLocation: 'middle', nameGap: 40 },
    // barGap '-100%' overlaps every series into one full-width slot per x; the
    // three restraint series occupy disjoint x positions, so they read as a
    // grouped chart while the violation series overlay each bar.
    series: [
      ...subs.map((s, k) => ({
        name: s.label,
        type: 'bar',
        data: restraint[k],
        itemStyle: { color: s.color },
        barGap: '-100%',
        barCategoryGap: '35%',
      })),
      {
        name: 'Violated',
        type: 'bar',
        data: violated,
        barGap: '-100%',
        itemStyle: { color: 'rgba(0,0,0,0.06)', decal: VIOL_DECAL },
      },
      {
        name: 'Consistently violated',
        type: 'bar',
        data: consistent,
        barGap: '-100%',
        itemStyle: { color: '#000' },
      },
    ],
  };
}

/** Bar chart of dihedral-angle restraints per angle type with the violated
 * (hatched) and consistently-violated (solid black) counts overlaid. The
 * aggregate "total" row is excluded (a summary, not a category). */
export function dihedViolationChart(rows: ViolationSummaryRow[]): object | null {
  const filtered = rows.filter((r) => (r.restraint_type ?? '').toLowerCase() !== 'total');
  if (!filtered.length) return null;
  const xLabels = filtered.map((r) => {
    const s = r.restraint_type ?? '';
    return s.charAt(0).toUpperCase() + s.slice(1);
  });
  return {
    tooltip: {
      trigger: 'item',
      formatter: (p: { name?: string; seriesName?: string; value?: number | null }) =>
        `${p.name} — ${p.seriesName}<br/>${p.value ?? 0}`,
    },
    legend: {
      orient: 'vertical',
      right: 8,
      top: 'middle',
      type: 'scroll',
      data: ['Violated', 'Consistently violated'],
    },
    grid: { left: 56, right: 170, top: 24, bottom: 24, containLabel: true },
    xAxis: { type: 'category', data: xLabels },
    yAxis: { type: 'value', name: 'Number of restraints', nameLocation: 'middle', nameGap: 40 },
    series: [
      {
        name: 'Restraints',
        type: 'bar',
        // Default palette color per angle type (matches the example).
        colorBy: 'data',
        data: filtered.map((r) => r.restraint_count ?? 0),
        barGap: '-100%',
        barCategoryGap: '45%',
      },
      {
        name: 'Violated',
        type: 'bar',
        data: filtered.map((r) => r.viol_count ?? 0),
        barGap: '-100%',
        itemStyle: { color: 'rgba(0,0,0,0.06)', decal: VIOL_DECAL },
      },
      {
        name: 'Consistently violated',
        type: 'bar',
        data: filtered.map((r) => r.consist_viol_count ?? 0),
        barGap: '-100%',
        itemStyle: { color: '#000' },
      },
    ],
  };
}

/** Dual-axis per-model chart: stacked violation counts by category (left axis)
 * with mean (circle) / median (×) markers and mean±SD error bars on the right
 * (violation magnitude) axis. Null when there are no per-model rows. */
export function modelViolationChartOption(
  rows: Record<string, number | null>[],
  unit: string,
  cats: ViolationCategory[],
): object | null {
  if (!rows.length) return null;
  const x = rows.map((r) => String(r['model_id']));
  const num = (v: number | null | undefined): number | null => (typeof v === 'number' ? v : null);
  const scatter = (key: string): (string | number)[][] =>
    rows
      .map((r) => [String(r['model_id']), num(r[key])] as (string | number | null)[])
      .filter((d) => d[1] !== null) as (string | number)[][];
  const errorData = rows
    .map((r) => {
      const m = num(r['mean_violation']);
      const s = num(r['std_violation']);
      return m !== null && s !== null
        ? ([String(r['model_id']), m - s, m + s] as (string | number)[])
        : null;
    })
    .filter((d): d is (string | number)[] => d !== null);
  // Extend the right axis so it covers the mean±SD error bars and median+SD
  // (a custom series doesn't drive axis auto-scaling on its own).
  let rightMax = 0;
  for (const r of rows) {
    const mean = num(r['mean_violation']);
    const median = num(r['median_violation']);
    const std = num(r['std_violation']);
    if (mean !== null && std !== null) rightMax = Math.max(rightMax, mean + std);
    if (median !== null && std !== null) rightMax = Math.max(rightMax, median + std);
  }
  const rightAxisMax = rightMax > 0 ? +(rightMax * 1.05).toFixed(2) : null;
  return {
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (
        params: {
          axisValue?: string;
          seriesName?: string;
          marker?: string;
          value?: number | (string | number)[];
        }[],
      ) => {
        const header = `Model ${params[0]?.axisValue ?? ''}`;
        const lines = params
          .filter((p) => p.seriesName !== 'Mean ± SD')
          .map((p) => {
            const v = Array.isArray(p.value) ? p.value[1] : p.value;
            // Median is plotted as an "×"; reuse the same glyph (rotated plus
            // path) in the tooltip so the marker matches the chart symbol.
            const marker =
              p.seriesName === 'Median'
                ? `<svg width="10" height="10" style="display:inline-block;vertical-align:middle;margin-right:5px"><path d="${MEDIAN_PATH}" fill="${MARK_COLOR}" transform="rotate(45 5 5)"/></svg>`
                : (p.marker ?? '');
            return `${marker}${p.seriesName}: ${v ?? ''}`;
          });
        return [header, ...lines].join('<br/>');
      },
    },
    legend: {
      orient: 'vertical',
      right: 8,
      top: 'middle',
      type: 'scroll',
      data: cats.map((c) => c.label),
    },
    grid: { left: 60, right: 200, top: 30, bottom: 56, containLabel: true },
    xAxis: {
      type: 'category',
      data: x,
      name: 'Model ID',
      nameLocation: 'middle',
      nameGap: 32,
      axisLabel: { fontSize: 9 },
    },
    yAxis: [
      {
        type: 'value',
        name: 'Number of violations',
        nameLocation: 'middle',
        nameGap: 44,
        splitNumber: 4,
      },
      {
        type: 'value',
        name: `Mean, median violations (${unit})`,
        nameLocation: 'middle',
        nameGap: 44,
        position: 'right',
        min: 0,
        ...(rightAxisMax !== null ? { max: rightAxisMax } : {}),
        splitNumber: 4,
        // Default axis color; hide its grid lines so only the left axis draws them.
        splitLine: { show: false },
      },
    ],
    series: [
      ...cats.map((c) => ({
        name: c.label,
        type: 'bar',
        stack: 'v',
        yAxisIndex: 0,
        ...(c.color ? { itemStyle: { color: c.color } } : {}),
        data: rows.map((r) => num(r[c.key]) ?? 0),
      })),
      {
        name: 'Mean',
        type: 'scatter',
        yAxisIndex: 1,
        symbol: 'circle',
        symbolSize: 8,
        itemStyle: { color: MARK_COLOR },
        data: scatter('mean_violation'),
      },
      {
        name: 'Median',
        type: 'scatter',
        yAxisIndex: 1,
        symbol: MEDIAN_SYMBOL,
        symbolRotate: 45,
        symbolSize: 9,
        itemStyle: { color: MARK_COLOR },
        data: scatter('median_violation'),
      },
      {
        name: 'Mean ± SD',
        type: 'custom',
        yAxisIndex: 1,
        silent: true,
        z: 5,
        renderItem: errorBarRenderItem,
        data: errorData,
      },
    ],
  };
}
