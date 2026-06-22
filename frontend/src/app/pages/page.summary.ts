import {
  Component,
  ElementRef,
  OnDestroy,
  computed,
  effect,
  inject,
  signal,
  viewChild,
} from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { FormsModule } from '@angular/forms';
import { CardModule } from 'primeng/card';
import { TableModule } from 'primeng/table';
import { PanelModule } from 'primeng/panel';
import { CheckboxModule } from 'primeng/checkbox';

import { PageService, TargetDepsys } from './page.service';
import { API_URL } from '../../site.config';
import { fileTypeLabel } from './file-types';
import { MolstarViewer } from './molstar';
import { EchartComponent } from './echart.component';

/** A selected upload file participating in the latest conversion run. */
interface UploadFileRow {
  original_name: string;
  file_size: number;
  file_type: string;
  source: string;
  /** Upload time as a naive UTC string ("YYYY-MM-DD HH:mm"); see GET /api/files. */
  uploaded_at: string | null;
}

/** A nested planes row: the plane's curated cells plus its outlier atoms. */
interface NestedRow {
  cells: string[];
  atoms: string[];
}

/** One geometry-validation metric table (see GET /api/coordinate_validation). */
interface ValidationMetric {
  key: string;
  label: string;
  count: number;
  columns: string[];
  /** Flat metrics: string[][]; nested (planes): NestedRow[]. */
  rows: string[][] | NestedRow[];
  nested?: boolean;
}

/** One row of an NMR error/warning table (location + description are HTML). */
interface NmrRow {
  location: string;
  description: string;
  active: boolean;
}

/** An NMR error group (one error type) — see GET /api/nmr_validation. */
interface NmrErrorGroup {
  type: string;
  title: string;
  real: boolean;
  count: number;
  rows: NmrRow[];
}

/** An NMR warning group (one warning type), with severity level + color. */
interface NmrWarningGroup {
  type: string;
  title: string;
  level: number;
  color: string;
  count: number;
  corrected: boolean;
  rows: NmrRow[];
}

// ── NMR data preview (graphical overview) — see GET /api/nmr_preview ───────────
interface HistogramChart {
  label: string;
  categories: string[];
  series: { name: string; data: number[] }[];
}
interface DihedralPlot {
  points: { name: string; x: number; y: number }[];
  /** Each error array is [x, y, x_low, x_high, y_low, y_high] (absolute). */
  errors: number[][];
}
interface DihedralChart {
  label: string;
  phi_psi?: DihedralPlot;
  chi1_chi2?: DihedralPlot;
}
interface NmrPreviewSource {
  name: string;
  content_name: string;
  subtypes: string[];
}
interface NmrCompleteness {
  chain: string;
  coverage_pct: number | null;
  groups: { group: string; target: number; assigned: number; pct: number }[];
}
/** Per-chain per-residue stacked counts + secondary-structure bands. */
interface PerResidueChart {
  chain: string;
  label: string;
  categories: string[];
  series: { name: string; data: number[] }[];
  bands: { start: number; end: number; type: string }[];
}
interface RestraintRow {
  type: string;
  name: string;
  total: number;
  range: string;
}
/** Contact map: per chain, one series of [seq_id_1, seq_id_2, total] per type. */
interface ContactMapChart {
  chain: string;
  label: string;
  min: number;
  max: number;
  series: { name: string; points: number[][] }[];
}
interface SpectralPeakSummary {
  name: string;
  exp_class: string;
  n_dims: number;
  n_peaks: number;
}
interface SpectralDimTable {
  name: string;
  rows: { id: number; atom: string; region: string; sweep_width: number | null; units: string }[];
}
/** Per-residue value line chart (dihedral angles, RDC, RCI/S²/RMSD). */
interface PerResidueLine {
  chain: string;
  label: string;
  sf?: string;
  categories: string[];
  series: { name: string; data: (number | null)[] }[];
  bands: { start: number; end: number; type: string }[];
  ymin: number | null;
  ymax: number | null;
  threshold: number | null;
}
/** Asymmetric (inter-chain) contact map: distinct x/y residue ranges. */
interface AsymContactMap {
  chain1: string;
  chain2: string;
  label: string;
  xmin: number;
  xmax: number;
  ymin: number;
  ymax: number;
  series: { name: string; points: number[][] }[];
}
interface NmrPreview {
  available: boolean;
  sources: NmrPreviewSource[];
  charts: {
    chem_shift_histogram: HistogramChart[];
    dist_histogram: HistogramChart[];
    dist_discrepancy: HistogramChart[];
    rdc_histogram: HistogramChart[];
    dihedral: DihedralChart[];
    per_residue: PerResidueChart[];
    contact_maps: ContactMapChart[];
    asym_contact_maps: AsymContactMap[];
    dihedral_per_residue: PerResidueLine[];
    rdc_per_residue: PerResidueLine[];
    rci: PerResidueLine[];
  };
  restraints: RestraintRow[];
  spectral_peaks: { summary: SpectralPeakSummary[]; dims: SpectralDimTable[] };
  completeness: NmrCompleteness[];
}

/** One ECharts panel: a title + the option object fed to <app-echart>. */
interface ChartPanel {
  title: string;
  option: object;
}

@Component({
  selector: 'app-summary',
  imports: [FormsModule, CardModule, TableModule, PanelModule, CheckboxModule, EchartComponent],
  templateUrl: './page.summary.html',
})
export class Summary implements OnDestroy {
  private pageService = inject(PageService);
  private http = inject(HttpClient);

  /** Selected files of the latest run, ordered by upload time (server-side). */
  files = signal<UploadFileRow[]>([]);

  /** The Source column is shown only when at least one file did not come from
   * the user (i.e. was downloaded from BMRB). */
  showSource = computed(() => this.files().some((f) => f.source !== 'user'));

  /** The coordinate preview is shown only for targets that produce a converted
   * coordinate file (onedep / repl_cs); bmrbdep has none. */
  showViewer = computed(() => {
    const t = this.pageService.pageState().targetDepsys;
    return t === TargetDepsys.onedep || t === TargetDepsys.repl_cs;
  });

  /** Set when the Mol* preview could not be loaded (e.g. no coordinate yet). */
  viewerError = signal(false);

  /** Coordinate geometry validation: null = loading, false = no coordinate. */
  validationAvailable = signal<boolean | null>(null);
  validationMetrics = signal<ValidationMetric[]>([]);
  /** True when the converted coordinate carried at least one outlier metric. */
  hasOutliers = computed(() => this.validationMetrics().length > 0);

  /** NMR data validation (all modes): null = loading, false = no report yet. */
  nmrAvailable = signal<boolean | null>(null);
  nmrStatus = signal<string | null>(null);
  nmrErrors = signal<NmrErrorGroup[]>([]);
  nmrWarnings = signal<NmrWarningGroup[]>([]);
  /** True when the NMR report carried at least one error or warning group. */
  hasNmrIssues = computed(() => this.nmrErrors().length > 0 || this.nmrWarnings().length > 0);

  /** NMR data preview (graphical overview): null = loading, false = no report. */
  nmrPreviewAvailable = signal<boolean | null>(null);
  private nmrPreview = signal<NmrPreview | null>(null);

  /** Data-summary / completeness / restraint / spectral-peak tables. */
  previewSources = computed(() => this.nmrPreview()?.sources ?? []);
  previewCompleteness = computed(() => this.nmrPreview()?.completeness ?? []);
  previewRestraints = computed(() => this.nmrPreview()?.restraints ?? []);
  previewSpectralSummary = computed(() => this.nmrPreview()?.spectral_peaks.summary ?? []);
  previewSpectralDims = computed(() => this.nmrPreview()?.spectral_peaks.dims ?? []);

  /** ECharts panels (built from the endpoint data). */
  chemShiftPanels = computed<ChartPanel[]>(() =>
    (this.nmrPreview()?.charts.chem_shift_histogram ?? []).map((h) => ({
      title: 'Normalized assigned chemical shifts (Z-score)',
      option: this.histogramOption(h, 'Z-score', '# of chemical shifts'),
    })),
  );
  distPanels = computed<ChartPanel[]>(() =>
    (this.nmrPreview()?.charts.dist_histogram ?? []).map((h) => ({
      title: 'Distance restraint target values',
      option: this.histogramOption(h, 'Distance (Å)', '# of distance restraints'),
    })),
  );
  dihedralPanels = computed<ChartPanel[]>(() => {
    const panels: ChartPanel[] = [];
    for (const d of this.nmrPreview()?.charts.dihedral ?? []) {
      if (d.phi_psi) panels.push({ title: 'φ / ψ dihedral angles', option: this.dihedralOption(d.phi_psi, 'φ', 'ψ') });
      if (d.chi1_chi2) panels.push({ title: 'χ1 / χ2 dihedral angles', option: this.dihedralOption(d.chi1_chi2, 'χ1', 'χ2') });
    }
    return panels;
  });
  discrepancyPanels = computed<ChartPanel[]>(() =>
    (this.nmrPreview()?.charts.dist_discrepancy ?? []).map((h) => ({
      title: 'Discrepancy in redundant distance restraints',
      option: this.histogramOption(h, 'Normalized discrepancy (%)', '# of redundant restraints'),
    })),
  );
  perResiduePanels = computed<ChartPanel[]>(() =>
    (this.nmrPreview()?.charts.per_residue ?? []).map((c) => ({
      title: `Distance restraints per residue — chain ${c.chain}`,
      option: this.perResidueOption(c),
    })),
  );
  rdcPanels = computed<ChartPanel[]>(() =>
    (this.nmrPreview()?.charts.rdc_histogram ?? []).map((h) => ({
      title: 'Observed RDC values',
      option: this.histogramOption(h, 'Obs. RDC value (Hz)', '# of RDC restraints'),
    })),
  );
  contactMapPanels = computed<ChartPanel[]>(() =>
    (this.nmrPreview()?.charts.contact_maps ?? []).map((c) => ({
      title: `Distance restraints contact map — chain ${c.chain}`,
      option: this.contactMapOption(c),
    })),
  );
  asymContactMapPanels = computed<ChartPanel[]>(() =>
    (this.nmrPreview()?.charts.asym_contact_maps ?? []).map((c) => ({
      title: `Inter-chain contact map — chains ${c.chain1} / ${c.chain2}`,
      option: this.asymContactMapOption(c),
    })),
  );
  dihedralPerResiduePanels = computed<ChartPanel[]>(() =>
    (this.nmrPreview()?.charts.dihedral_per_residue ?? []).map((c) => ({
      title: `Dihedral angles per residue — chain ${c.chain}`,
      option: this.lineOption(c),
    })),
  );
  rdcPerResiduePanels = computed<ChartPanel[]>(() =>
    (this.nmrPreview()?.charts.rdc_per_residue ?? []).map((c) => ({
      title: `Observed RDC per residue — chain ${c.chain}`,
      option: this.lineOption(c),
    })),
  );
  rciPanels = computed<ChartPanel[]>(() =>
    (this.nmrPreview()?.charts.rci ?? []).map((c) => ({
      title: `${c.label} — chain ${c.chain}${c.sf ? ' (' + c.sf + ')' : ''}`,
      option: this.lineOption(c),
    })),
  );

  /** True when the preview has any chart or table content to show. */
  hasPreviewContent = computed(
    () =>
      this.chemShiftPanels().length > 0 ||
      this.distPanels().length > 0 ||
      this.discrepancyPanels().length > 0 ||
      this.rdcPanels().length > 0 ||
      this.dihedralPanels().length > 0 ||
      this.perResiduePanels().length > 0 ||
      this.contactMapPanels().length > 0 ||
      this.asymContactMapPanels().length > 0 ||
      this.dihedralPerResiduePanels().length > 0 ||
      this.rdcPerResiduePanels().length > 0 ||
      this.rciPanels().length > 0 ||
      this.previewCompleteness().length > 0 ||
      this.previewRestraints().length > 0 ||
      this.previewSpectralSummary().length > 0 ||
      this.previewSources().length > 0,
  );

  // ── Warning acknowledgment / download approval (Terms #7) ───────────────────
  /** Keys of the tables the user has acknowledged. */
  private acknowledged = signal<Set<string>>(new Set());

  /** An NMR error group is acknowledgeable only when it is a *potential* (non-real)
   * error; real/blocking errors must be fixed, not acknowledged. */
  nmrErrAck(g: NmrErrorGroup): boolean {
    return !g.real;
  }
  /** An NMR warning group is acknowledgeable for levels 1–4 (level 0 = already
   * remediated, nothing to acknowledge). */
  nmrWarnAck(g: NmrWarningGroup): boolean {
    return g.level >= 1;
  }

  /** Stable acknowledgment keys for every acknowledgeable table on the page. */
  private acknowledgeableKeys = computed<string[]>(() => {
    const keys: string[] = [];
    if (this.showViewer()) {
      for (const m of this.validationMetrics()) keys.push('geo:' + m.key);
    }
    for (const g of this.nmrErrors()) if (this.nmrErrAck(g)) keys.push('nmrerr:' + g.type);
    for (const g of this.nmrWarnings()) if (this.nmrWarnAck(g)) keys.push('nmrwarn:' + g.type);
    return keys;
  });

  /** A real (blocking) NMR error is present → download is blocked; cannot approve. */
  hasBlockingError = computed(() => this.nmrErrors().some((g) => g.real));

  /** Download is allowed: no blocking error and every acknowledgeable table checked. */
  canApprove = computed(
    () =>
      !this.hasBlockingError() &&
      this.acknowledgeableKeys().every((k) => this.acknowledged().has(k)),
  );

  /** Read-only after download. */
  locked = computed(() => this.pageService.pageState().downloaded);

  /** A conversion run exists (the validation/approval UI only applies then). */
  processed = computed(() => this.pageService.pageState().conversionId !== null);

  /** Host element for the Mol* canvas (only present while showViewer()). */
  private coordinateHost = viewChild<ElementRef<HTMLDivElement>>('molstarHost');

  private fetched = false;
  private validationFetched = false;
  private nmrFetched = false;
  private nmrPreviewFetched = false;
  private ackInitialized = false;
  private viewerInit = false;
  private viewer: MolstarViewer | null = null;

  /** Shared, one-shot loader for the prebuilt Mol* bundle assets. */
  private static molstarLoaded?: Promise<void>;

  constructor() {
    // Load the file list once the session token is available (covers direct
    // navigation, refresh, and arriving from the processing dialog).
    effect(() => {
      const token = this.pageService.pageState().tokenBase;
      if (token && !this.fetched) {
        this.fetched = true;
        this.loadFiles(token);
      }
    });

    // Initialise the Mol* coordinate preview once the token is known, the
    // target qualifies, and the host element has been rendered (viewChild is a
    // signal, so this effect re-runs when the @if reveals the host).
    effect(() => {
      const token = this.pageService.pageState().tokenBase;
      const host = this.coordinateHost()?.nativeElement;
      if (!token || !this.showViewer() || !host || this.viewerInit) return;
      this.viewerInit = true;
      void this.initViewer(token, host);
    });

    // Load the coordinate geometry-validation report once the token is known and
    // the target produces a coordinate (onedep / repl_cs).
    effect(() => {
      const token = this.pageService.pageState().tokenBase;
      if (!token || !this.showViewer() || this.validationFetched) return;
      this.validationFetched = true;
      this.loadValidation(token);
    });

    // Load the NMR data validation report once the token is known (all modes).
    effect(() => {
      const token = this.pageService.pageState().tokenBase;
      if (!token || this.nmrFetched) return;
      this.nmrFetched = true;
      this.loadNmrValidation(token);
    });

    // Load the NMR data preview (graphical overview) once the token is known.
    effect(() => {
      const token = this.pageService.pageState().tokenBase;
      if (!token || this.nmrPreviewFetched) return;
      this.nmrPreviewFetched = true;
      this.loadNmrPreview(token);
    });

    // Initialise acknowledgment state once the run is processed and both
    // validation reports have resolved: restore checks from session.approved, or
    // (OK case) approve immediately when there is nothing to acknowledge.
    effect(() => {
      const state = this.pageService.pageState();
      const geoReady = !this.showViewer() || this.validationAvailable() !== null;
      const nmrReady = this.nmrAvailable() !== null;
      if (state.conversionId === null || !geoReady || !nmrReady || this.ackInitialized) return;
      this.ackInitialized = true;
      if (state.approved) {
        this.acknowledged.set(new Set(this.acknowledgeableKeys()));
      } else if (this.acknowledgeableKeys().length === 0 && !this.hasBlockingError()) {
        this.patchApproved(true);
      }
    });
  }

  ngOnDestroy(): void {
    this.disposeViewer();
  }

  private loadFiles(token: string): void {
    this.http
      .get<{ files: UploadFileRow[] }>(API_URL + 'files', { params: { token } })
      .subscribe({
        next: (res) => this.files.set(res.files ?? []),
        error: (err) => console.error('Failed to load upload files', err),
      });
  }

  private loadValidation(token: string): void {
    this.http
      .get<{
        available: boolean;
        metrics: ValidationMetric[];
      }>(API_URL + 'coordinate_validation', { params: { token } })
      .subscribe({
        next: (res) => {
          this.validationMetrics.set(res.metrics ?? []);
          this.validationAvailable.set(res.available);
        },
        error: (err) => {
          console.error('Failed to load coordinate validation', err);
          this.validationAvailable.set(false);
        },
      });
  }

  private loadNmrValidation(token: string): void {
    this.http
      .get<{
        available: boolean;
        status: string | null;
        errors: NmrErrorGroup[];
        warnings: NmrWarningGroup[];
      }>(API_URL + 'nmr_validation', { params: { token } })
      .subscribe({
        next: (res) => {
          this.nmrErrors.set(res.errors ?? []);
          this.nmrWarnings.set(res.warnings ?? []);
          this.nmrStatus.set(res.status ?? null);
          this.nmrAvailable.set(res.available);
        },
        error: (err) => {
          console.error('Failed to load NMR validation', err);
          this.nmrAvailable.set(false);
        },
      });
  }

  private loadNmrPreview(token: string): void {
    this.http
      .get<NmrPreview>(API_URL + 'nmr_preview', { params: { token } })
      .subscribe({
        next: (res) => {
          this.nmrPreview.set(res);
          this.nmrPreviewAvailable.set(res.available);
        },
        error: (err) => {
          console.error('Failed to load NMR preview', err);
          this.nmrPreviewAvailable.set(false);
        },
      });
  }

  /** ECharts option for a stacked-bar histogram. */
  private histogramOption(h: HistogramChart, xName: string, yName: string): object {
    return {
      title: { text: h.label, left: 'center', textStyle: { fontSize: 12, fontWeight: 'normal' } },
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
      legend: { bottom: 0, type: 'scroll' },
      grid: { left: 56, right: 16, top: 36, bottom: 64, containLabel: true },
      xAxis: {
        type: 'category',
        data: h.categories,
        name: xName,
        nameLocation: 'middle',
        nameGap: 40,
        axisLabel: { rotate: -75, fontSize: 9 },
      },
      yAxis: { type: 'value', name: yName, minInterval: 1 },
      series: h.series.map((s) => ({ name: s.name, type: 'bar', stack: 'total', data: s.data })),
    };
  }

  /** ECharts option for a dihedral scatter with custom bidirectional error bars
   * (a `custom` series — no third-party plugin). */
  private dihedralOption(plot: DihedralPlot, xName: string, yName: string): object {
    const axis = (name: string) => ({
      type: 'value' as const,
      name,
      min: -180,
      max: 180,
      interval: 90,
      splitLine: { show: true },
    });
    return {
      tooltip: {
        trigger: 'item',
        formatter: (p: { data?: { name?: string; value?: number[] } }) =>
          p.data?.value
            ? `${p.data.name ?? ''}<br/>${xName}: ${p.data.value[0]}°<br/>${yName}: ${p.data.value[1]}°`
            : '',
      },
      grid: { left: 56, right: 24, top: 24, bottom: 48, containLabel: true },
      xAxis: axis(`${xName} (°)`),
      yAxis: axis(`${yName} (°)`),
      series: [
        {
          // Bidirectional error bars drawn beneath the points.
          type: 'custom',
          silent: true,
          z: 1,
          data: plot.errors,
          encode: { x: 0, y: 1 },
          renderItem: this.errorBarRenderItem,
        },
        {
          type: 'scatter',
          z: 2,
          symbolSize: 6,
          itemStyle: { color: '#2563eb', opacity: 0.7 },
          data: plot.points.map((pt) => ({ name: pt.name, value: [pt.x, pt.y] })),
        },
      ],
    };
  }

  /** Translucent background color for a secondary-structure band. */
  private bandColor(type: string): string {
    if (type === 'helix') return 'rgba(204,47,0,0.12)';
    if (type === 'strand') return 'rgba(0,156,209,0.12)';
    if (type === 'turn') return 'rgba(200,204,0,0.18)';
    return 'rgba(120,120,120,0.08)';
  }

  /** ECharts option for a per-residue stacked-count bar with secondary-structure
   * bands drawn as markAreas. */
  private perResidueOption(c: PerResidueChart): object {
    const markArea = {
      silent: true,
      data: c.bands.map((b) => [
        { xAxis: c.categories[b.start], itemStyle: { color: this.bandColor(b.type) } },
        { xAxis: c.categories[b.end] },
      ]),
    };
    const interval = Math.max(0, Math.ceil(c.categories.length / 24) - 1);
    return {
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
      legend: { bottom: 0, type: 'scroll' },
      grid: { left: 48, right: 16, top: 24, bottom: 72, containLabel: true },
      xAxis: { type: 'category', data: c.categories, axisLabel: { interval, rotate: -75, fontSize: 8 } },
      yAxis: { type: 'value', name: '# restraints', minInterval: 1 },
      series: c.series.map((s, idx) => ({
        name: s.name,
        type: 'bar',
        stack: 'total',
        data: s.data,
        ...(idx === 0 ? { markArea } : {}),
      })),
    };
  }

  /** ECharts option for a symmetric contact map: scatter of [seq1, seq2] points
   * sized by restraint count, on a square residue×residue grid (y inverted). */
  private contactMapOption(c: ContactMapChart): object {
    const axis = (name: string) => ({ type: 'value' as const, name, min: c.min, max: c.max, minInterval: 1 });
    return {
      tooltip: {
        trigger: 'item',
        formatter: (p: { data?: number[] }) =>
          p.data ? `${p.data[0]} ↔ ${p.data[1]}<br/>count: ${p.data[2]}` : '',
      },
      legend: { bottom: 0, type: 'scroll' },
      grid: { left: 48, right: 24, top: 16, bottom: 48, containLabel: true },
      xAxis: axis('Residue'),
      yAxis: { ...axis('Residue'), inverse: true },
      series: c.series.map((s) => ({
        name: s.name,
        type: 'scatter',
        data: s.points,
        symbolSize: (v: number[]) => Math.min(16, 4 + 2 * (v[2] || 1)),
      })),
    };
  }

  /** ECharts option for an asymmetric (inter-chain) contact map: scatter with
   * independent x (chain 1) and y (chain 2) residue ranges, sized by count. */
  private asymContactMapOption(c: AsymContactMap): object {
    return {
      tooltip: {
        trigger: 'item',
        formatter: (p: { data?: number[] }) =>
          p.data ? `${c.chain1}:${p.data[0]} ↔ ${c.chain2}:${p.data[1]}<br/>count: ${p.data[2]}` : '',
      },
      legend: { bottom: 0, type: 'scroll' },
      grid: { left: 48, right: 24, top: 16, bottom: 48, containLabel: true },
      xAxis: { type: 'value', name: `Chain ${c.chain1}`, min: c.xmin, max: c.xmax, minInterval: 1 },
      yAxis: { type: 'value', name: `Chain ${c.chain2}`, min: c.ymin, max: c.ymax, minInterval: 1 },
      series: c.series.map((s) => ({
        name: s.name,
        type: 'scatter',
        data: s.points,
        symbolSize: (v: number[]) => Math.min(16, 4 + 2 * (v[2] || 1)),
      })),
    };
  }

  /** ECharts option for a per-residue value line chart (dihedral / RDC / RCI),
   * with secondary-structure bands and an optional threshold line. */
  private lineOption(c: PerResidueLine): object {
    const interval = Math.max(0, Math.ceil(c.categories.length / 24) - 1);
    const markArea = {
      silent: true,
      data: c.bands.map((b) => [
        { xAxis: c.categories[b.start], itemStyle: { color: this.bandColor(b.type) } },
        { xAxis: c.categories[b.end] },
      ]),
    };
    const markLine =
      c.threshold !== null
        ? { silent: true, symbol: 'none', data: [{ yAxis: c.threshold }],
            lineStyle: { color: '#dc2626', type: 'dashed' } }
        : undefined;
    return {
      tooltip: { trigger: 'axis' },
      legend: { bottom: 0, type: 'scroll' },
      grid: { left: 52, right: 16, top: 24, bottom: 64, containLabel: true },
      xAxis: { type: 'category', data: c.categories, axisLabel: { interval, rotate: -75, fontSize: 8 } },
      yAxis: {
        type: 'value',
        ...(c.ymin !== null ? { min: c.ymin } : {}),
        ...(c.ymax !== null ? { max: c.ymax } : {}),
      },
      series: c.series.map((s, idx) => ({
        name: s.name,
        type: 'line',
        data: s.data,
        connectNulls: false,
        showSymbol: false,
        ...(idx === 0 ? { markArea, ...(markLine ? { markLine } : {}) } : {}),
      })),
    };
  }

  /** renderItem for the dihedral error-bar `custom` series: a horizontal line
   * (x_low→x_high at y) and a vertical line (y_low→y_high at x). */
  private errorBarRenderItem = (
    _params: unknown,
    api: { value(i: number): number; coord(p: number[]): number[] },
  ): object => {
    const x = api.value(0);
    const y = api.value(1);
    const xlo = api.coord([api.value(2), y]);
    const xhi = api.coord([api.value(3), y]);
    const ylo = api.coord([x, api.value(4)]);
    const yhi = api.coord([x, api.value(5)]);
    const style = { stroke: 'rgba(37,99,235,0.25)', lineWidth: 1 };
    return {
      type: 'group',
      children: [
        { type: 'line', shape: { x1: xlo[0], y1: xlo[1], x2: xhi[0], y2: xhi[1] }, style },
        { type: 'line', shape: { x1: ylo[0], y1: ylo[1], x2: yhi[0], y2: yhi[1] }, style },
      ],
    };
  };

  /** Whether a table (by key) has been acknowledged. */
  isAcked(key: string): boolean {
    return this.acknowledged().has(key);
  }

  /** Toggle a table's acknowledgment and persist the resulting approval state.
   * No-op once the session is locked (downloaded). */
  toggleAck(key: string): void {
    if (this.locked()) return;
    const next = new Set(this.acknowledged());
    if (next.has(key)) {
      next.delete(key);
    } else {
      next.add(key);
    }
    this.acknowledged.set(next);
    this.patchApproved(this.canApprove());
  }

  /** Persist session.approved (POST /api/approve) and reflect it in page state. */
  private patchApproved(value: boolean): void {
    const token = this.pageService.pageState().tokenBase;
    if (!token || this.pageService.pageState().approved === value) return;
    this.http
      .post(API_URL + 'approve', { token, approved: value })
      .subscribe({
        next: () =>
          this.pageService.pageState.update((prev) => ({ ...prev, approved: value })),
        error: (err) => console.error('Failed to update approval', err),
      });
  }

  /** Typed row accessors for the template (rows is a flat/nested union). */
  flatRows(m: ValidationMetric): string[][] {
    return m.rows as string[][];
  }

  nestedRows(m: ValidationMetric): NestedRow[] {
    return m.rows as NestedRow[];
  }

  /** Per-plane atom-row expansion state (only the planes metric is nested). */
  private expandedPlanes = signal<Set<number>>(new Set());

  togglePlane(i: number): void {
    const next = new Set(this.expandedPlanes());
    if (next.has(i)) {
      next.delete(i);
    } else {
      next.add(i);
    }
    this.expandedPlanes.set(next);
  }

  isPlaneExpanded(i: number): boolean {
    return this.expandedPlanes().has(i);
  }

  /** Lazily inject the prebuilt Mol* bundle (served at /molstar/) once. */
  private loadMolstarAssets(): Promise<void> {
    if (Summary.molstarLoaded) return Summary.molstarLoaded;
    Summary.molstarLoaded = new Promise<void>((resolve, reject) => {
      const link = document.createElement('link');
      link.rel = 'stylesheet';
      link.href = '/molstar/molstar.css';
      document.head.appendChild(link);

      const script = document.createElement('script');
      script.src = '/molstar/molstar.js';
      script.onload = () => resolve();
      script.onerror = () => reject(new Error('Failed to load Mol*'));
      document.head.appendChild(script);
    });
    return Summary.molstarLoaded;
  }

  /** Create the Mol* viewer and load the converted coordinate (mmCIF). */
  private async initViewer(token: string, host: HTMLElement): Promise<void> {
    try {
      await this.loadMolstarAssets();
      if (!window.molstar) throw new Error('Mol* global unavailable');
      const viewer = await window.molstar.Viewer.create(host, {
        layoutIsExpanded: false,
        layoutShowControls: false,
        layoutShowRemoteState: false,
        layoutShowSequence: false,
        layoutShowLog: false,
        layoutShowLeftPanel: false,
        viewportShowExpand: true, // built-in fullscreen toggle
        viewportShowSelectionMode: false,
        viewportShowAnimation: false,
      });
      this.viewer = viewer;
      const url = `${API_URL}coordinate?token=${encodeURIComponent(token)}`;
      // Rejects on a 404 (no coordinate) → caught below to show the fallback.
      await viewer.loadStructureFromUrl(url, 'mmcif', false);
    } catch (err) {
      console.error('Mol* coordinate preview unavailable', err);
      this.viewerError.set(true);
      this.disposeViewer();
    }
  }

  /** Dispose the Mol* viewer (frees its WebGL context + workers). */
  private disposeViewer(): void {
    try {
      this.viewer?.dispose();
    } catch {
      /* already torn down */
    }
    this.viewer = null;
  }

  /** Human-readable file-type label. */
  typeLabel(value: string): string {
    return fileTypeLabel(value);
  }

  sourceLabel(source: string): string {
    return source === 'bmrb' ? 'BMRB' : 'User';
  }

  formatSize(bytes: number): string {
    if (!bytes) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(1024));
    return `${(bytes / Math.pow(1024, i)).toFixed(1)} ${units[i]}`;
  }
}
