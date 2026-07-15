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
import { Router } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { CardModule } from 'primeng/card';
import { TableModule } from 'primeng/table';
import { PanelModule } from 'primeng/panel';
import { CheckboxModule } from 'primeng/checkbox';
import { ButtonModule } from 'primeng/button';
import { MessageModule } from 'primeng/message';
import { TooltipModule } from 'primeng/tooltip';

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
  description: string;
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
  /** Outlier markers (chem-shift Z scores): dashed line + short description.
   * `x` is the precise fractional category-axis index of the value. */
  annotations?: { x: number; anomalous: boolean; text: string }[];
}
interface DihedralPlot {
  /** Points grouped by residue type (comp_id); each group is one scatter series
   * (plus an error-bar series sharing its name, so the legend toggles both).
   * `seq_id` labels the point on hover; each error array is
   * [x, y, x_low, x_high, y_low, y_high] (absolute). */
  groups: {
    comp_id: string;
    points: { x: number; y: number; seq_id: string | number }[];
    errors: number[][];
  }[];
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
  categories: {
    label: string;
    groups: { group: string; target: number; assigned: number; pct: number }[];
  }[];
  excluded_comp_id: { seq_id: number; comp_id: string }[];
  excluded_atom_id: { seq_id: number; comp_id: string; atom_id: string; value: number | null }[];
}
/** Per-chain per-residue stacked counts + secondary-structure bands. */
interface PerResidueChart {
  chain: string;
  label: string;
  categories: string[];
  series: { name: string; data: number[] }[];
  bands: { start: number; end: number; type: string; label: string }[];
}
/** A secondary-structure region on a map axis; start/end are residue seq_id
 * values (the band spans [start-0.5, end+0.5]). */
interface MapBand {
  start: number;
  end: number;
  type: string;
  label: string;
}
/** One contact-map point: value = [seq_id_1, seq_id_2, total]; c1/c2 are the two
 * residues' names (comp_id), for the "<comp> <seq>" tooltip label. */
interface ContactPoint {
  value: number[];
  c1: string;
  c2: string;
}
/** Contact map: per chain, one series of contact points per type. `bands` are
 * the secondary-structure regions, drawn on both axes. */
interface ContactMapChart {
  chain: string;
  label: string;
  min: number;
  max: number;
  series: { name: string; points: ContactPoint[] }[];
  bands: MapBand[];
}
/** One spectral-peak-list saveframe's preview content, in display order. */
interface SpectralPeakSaveframe {
  sf_framecode: string;
  status: string | null;
  error_descriptions: string[];
  warning_descriptions: string[];
  sequence_coverage: SeqCoverageRow[];
  exp_class: string;
  n_dims: number;
  n_peaks: number;
  peak_counts: { label: string; count: number }[];
  dims: { id: number; atom: string; region: string; sweep_width: number | null; units: string }[];
  atom_name_mapping: AtomNameMappingRow[];
}
/** Per-residue value line chart (dihedral angles, RDC, RCI/S²/RMSD). */
interface PerResidueLine {
  chain: string;
  label: string;
  sf?: string;
  categories: string[];
  series: { name: string; data: (number | null)[] }[];
  bands: { start: number; end: number; type: string; label: string }[];
  ymin: number | null;
  ymax: number | null;
  threshold: number | null;
}
/** Asymmetric (inter-chain) contact map: distinct x/y residue ranges. `xbands`
 * (chain 1) are drawn vertically, `ybands` (chain 2) horizontally. */
interface AsymContactMap {
  chain1: string;
  chain2: string;
  label: string;
  xmin: number;
  xmax: number;
  ymin: number;
  ymax: number;
  series: { name: string; points: ContactPoint[] }[];
  xbands: MapBand[];
  ybands: MapBand[];
}
/** A chemical-shift-prediction validation row (predicted vs coordinate state). */
interface PredictionRow {
  residue: string;
  shifts: string;
  predicted: string;
  observed: string;
  consistent: boolean | null;
}
interface CitationRow {
  title: string;
  authors: string;
  journal: string;
  doi: string;
}
interface AlignChainRow {
  chain: string;
  length: number;
  matched: number;
  conflict: number;
  unmapped: number;
  coverage: number | null;
  ref_gauge: string;
  ref: string;
  mid: string;
  test: string;
  test_gauge: string;
}
interface AlignGroup {
  category: string;
  rows: AlignChainRow[];
}
/** Per-residue (Comp_ID) atom-name mapping; each history entry maps an
 * author-defined atom name to its IUPAC Atom_ID(s); `unusual` flags an
 * unexpected pseudo-atom mapping (highlighted red). */
interface AtomNameMappingRow {
  comp_id: string;
  history: { name: string; atoms: string; unusual: boolean }[];
}
/** Per-chain sequence coverage of the experimental data for one saveframe. */
interface SeqCoverageRow {
  chain: string;
  length: number;
  coverage_pct: number | null;
  ref_gauge: string;
  ref: string;
  mid: string;
  test: string;
}
/** One assigned-chemical-shift saveframe's preview content, in display order. */
interface ChemShiftSaveframe {
  sf_framecode: string;
  status: string | null;
  error_descriptions: string[];
  warning_descriptions: string[];
  sequence_coverage: SeqCoverageRow[];
  assignments: { label: string; count: number }[];
  completeness: NmrCompleteness[];
  predictions: {
    cys_redox: PredictionRow[];
    pro_cis_trans: PredictionRow[];
    his_tautomer: PredictionRow[];
    ilv_rotamer: PredictionRow[];
  };
  histogram: HistogramChart[];
  rci: PerResidueLine[];
  atom_name_mapping: AtomNameMappingRow[];
}
/** One distance-restraint saveframe's preview content, in display order. */
interface DistRestraintSaveframe {
  sf_framecode: string;
  status: string | null;
  error_descriptions: string[];
  warning_descriptions: string[];
  exp_type: string;
  sequence_coverage: SeqCoverageRow[];
  /** Hierarchical lists (number / weight / potential type of constraints);
   * each `html` is a ready-to-render <ul> tree bound via [innerHTML]. */
  constraint_lists: { key: string; title: string; html: string }[];
  range: string;
  histogram: HistogramChart[];
  discrepancy: HistogramChart[];
  per_residue: PerResidueChart[];
  contact_maps: ContactMapChart[];
  asym_contact_maps: AsymContactMap[];
  atom_name_mapping: AtomNameMappingRow[];
}
/** One dihedral-angle-restraint saveframe's preview content, in display order. */
interface DihedRestraintSaveframe {
  sf_framecode: string;
  status: string | null;
  error_descriptions: string[];
  warning_descriptions: string[];
  exp_type: string;
  sequence_coverage: SeqCoverageRow[];
  /** Hierarchical lists (number / combined / … / weight / potential type of
   * constraints); each `html` is a ready-to-render <ul> tree (via [innerHTML]). */
  constraint_lists: { key: string; title: string; html: string }[];
  histogram: HistogramChart[];
  discrepancy: HistogramChart[];
  dihedral: DihedralChart[];
  per_residue: PerResidueLine[];
  atom_name_mapping: AtomNameMappingRow[];
}
/** One RDC-restraint saveframe's preview content, in display order. */
interface RdcRestraintSaveframe {
  sf_framecode: string;
  status: string | null;
  error_descriptions: string[];
  warning_descriptions: string[];
  exp_type: string;
  sequence_coverage: SeqCoverageRow[];
  /** Hierarchical lists (number / … / weight / potential type of constraints);
   * each `html` is a ready-to-render <ul> tree (bound via [innerHTML]). */
  constraint_lists: { key: string; title: string; html: string }[];
  range: string;
  histogram: HistogramChart[];
  discrepancy: HistogramChart[];
  per_residue: PerResidueLine[];
  atom_name_mapping: AtomNameMappingRow[];
}
/** Global properties of the molecular assembly (NMR experiment environment). */
interface AssemblyProperties {
  diamagnetic: boolean | null;
  disulfide_bond: boolean | null;
  other_bond: boolean | null;
  cyclic_polymer: boolean | null;
  disulfide_bonds: { atom1: string; atom2: string; distance: number | null }[];
  other_bonds: { type: string | null; atom1: string; atom2: string; distance: number | null }[];
  non_standard_residues: {
    chain: string;
    seq_id: number;
    comp_id: string;
    name: string | null;
    matched: boolean;
    exptl: string;
  }[];
}
/** One inventory row: a parsed saveframe within a processed NMR data file. */
interface NmrInventoryRow {
  list_id: number;
  subtype: string;
  subtype_unknown: boolean;
  sf_framecode: string;
  status: string | null;
  has_issue: boolean;
  is_error: boolean;
  n_rows: string;
  rows_zero: boolean;
  exp_type: string;
  exp_unknown: boolean;
  coverage: string;
  coverage_low: boolean;
  coverage_missing: boolean;
  coverage_required: boolean;
}
/** Inventory of one processed NMR data file (one table per input source). */
interface NmrInventoryFile {
  content_name: string;
  file_name: string;
  has_sets: boolean;
  rows: NmrInventoryRow[];
}
interface NmrPreview {
  available: boolean;
  sources: NmrPreviewSource[];
  /** Single inventory of what was parsed/interpreted, per processed file. */
  inventory: NmrInventoryFile[];
  /** Global properties of the molecular assembly. */
  assembly: AssemblyProperties;
  /** Assigned chemical shifts grouped by saveframe (sf_framecode). */
  chem_shift_saveframes: ChemShiftSaveframe[];
  /** Distance restraints grouped by saveframe (sf_framecode). */
  dist_restraint_saveframes: DistRestraintSaveframe[];
  /** Dihedral angle restraints grouped by saveframe (sf_framecode). */
  dihed_restraint_saveframes: DihedRestraintSaveframe[];
  /** RDC restraints grouped by saveframe (sf_framecode). */
  rdc_restraint_saveframes: RdcRestraintSaveframe[];
  /** Spectral peak lists grouped by saveframe (sf_framecode). */
  spectral_peak_saveframes: SpectralPeakSaveframe[];
  alignments: AlignGroup[];
  /** Coordinate ensemble composition (well-defined regions); null when absent. */
  ensemble_composition: EnsembleComposition | null;
}
/** One well-defined region of the coordinate ensemble (ensemble_composition). */
interface EnsembleRegion {
  domain_id?: number;
  medoid_model_id?: number;
  number_of_monomers?: number;
  percent_of_core?: number;
  medoid_rmsd?: number;
  range_of_seq_id?: string;
}
interface EnsembleComposition {
  well_defined_region?: EnsembleRegion[];
}
/** A titled chemical-shift-prediction table. */
interface PredictionTable {
  title: string;
  rows: PredictionRow[];
  tooltip: CitationRow[];
}

/** One ECharts panel: a title + the option object fed to <app-echart>. An
 * optional `aspect` (plot height / width) sizes the chart proportionally to its
 * data extent (contact maps, dihedral scatter); omitted panels fill the width. */
interface ChartPanel {
  title: string;
  option: object;
  aspect?: number;
  /** Chart chrome (px) so `aspect` applies to the plot area, not the whole box:
   * grid left+right (marginX) and top+bottom (marginY). */
  marginX?: number;
  marginY?: number;
}

@Component({
  selector: 'app-summary',
  imports: [
    FormsModule,
    CardModule,
    TableModule,
    PanelModule,
    CheckboxModule,
    ButtonModule,
    MessageModule,
    TooltipModule,
    EchartComponent,
  ],
  templateUrl: './page.summary.html',
  // Glow still-unacknowledged warning checkboxes to draw the eye (paused for
  // users who prefer reduced motion).
  styles: [
    `
      @keyframes ack-glow {
        0%,
        100% {
          box-shadow: 0 0 0 0 rgba(245, 158, 11, 0);
        }
        50% {
          box-shadow: 0 0 8px 3px rgba(245, 158, 11, 0.75);
        }
      }
      .ack-glow {
        border-radius: 6px;
        animation: ack-glow 1.4s ease-in-out infinite;
      }
      @media (prefers-reduced-motion: reduce) {
        .ack-glow {
          animation: none;
        }
      }
      /* Let the citation tooltip size to its content (overrides PrimeNG's
         default tooltip max-width). ::ng-deep reaches the body-appended
         tooltip element, which lives outside this component's DOM. */
      ::ng-deep .cite-tooltip .p-tooltip-text {
        width: fit-content;
      }
    `,
  ],
})
export class Summary implements OnDestroy {
  private pageService = inject(PageService);
  private http = inject(HttpClient);
  private router = inject(Router);

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

  /** Data-summary table + sequence alignments. */
  previewSources = computed(() => this.nmrPreview()?.sources ?? []);
  previewAlignments = computed(() => this.nmrPreview()?.alignments ?? []);

  /** Coordinate ensemble well-defined regions (shown under the coordinate preview). */
  ensembleRegions = computed<EnsembleRegion[]>(
    () => this.nmrPreview()?.ensemble_composition?.well_defined_region ?? [],
  );

  /** Per-file inventory of parsed NMR data (the single global summary). */
  previewInventory = computed(() => this.nmrPreview()?.inventory ?? []);

  /** Global molecular-assembly properties (NMR experiment environment). */
  previewAssembly = computed(() => this.nmrPreview()?.assembly ?? null);
  /** The four global properties as label/value rows for the summary table. */
  assemblyProps = computed(() => {
    const a = this.previewAssembly();
    if (!a) return [];
    const yn = (v: boolean | null) => (v === null ? '—' : v ? 'Yes' : 'No');
    return [
      {
        label: 'Diamagnetism of the molecular assembly',
        value: yn(a.diamagnetic),
        note: 'excluding oxygen atoms',
      },
      { label: 'Has a disulfide bond', value: yn(a.disulfide_bond), note: '' },
      { label: 'Has an other bond', value: yn(a.other_bond), note: '' },
      { label: 'Contains a cyclic polymer', value: yn(a.cyclic_polymer), note: '' },
    ];
  });

  /** Spectral peak lists grouped by saveframe (sf_framecode). */
  spectralPeakSaveframes = computed(() => this.nmrPreview()?.spectral_peak_saveframes ?? []);

  /** Assigned chemical shifts grouped by saveframe (sf_framecode). */
  chemShiftSaveframes = computed(() => this.nmrPreview()?.chem_shift_saveframes ?? []);

  predictionTablesOf(sf: ChemShiftSaveframe): PredictionTable[] {
    const p = sf.predictions;
    return [
      {
        title: 'Cysteine redox state',
        rows: p.cys_redox,
        tooltip: [
          {
            title: '13C NMR chemical shifts can predict disulfide bond formation.',
            authors: 'Sharma, D., Rajarathnam, K.',
            journal: 'J Biomol NMR 18, 165–171 (2000)',
            doi: '10.1023/A:1008398416292',
          },
        ],
      },
      {
        title: 'Proline cis/trans peptide bond',
        rows: p.pro_cis_trans,
        tooltip: [
          {
            title:
              'A software tool for the prediction of Xaa-Pro peptide bond conformations in proteins based on 13C chemical shift statistics.',
            authors: 'Schubert, M., Labudde, D., Oschkinat, H. et al.',
            journal: 'J Biomol NMR 24, 149–154 (2002)',
            doi: 'DOI: 10.1023/A:1020997118364',
          },
        ],
      },
      {
        title: 'Histidine tautomeric state',
        rows: p.his_tautomer,
        tooltip: [
          {
            title:
              'Protonation, Tautomerization, and Rotameric Structure of Histidine: A Comprehensive Study by Magic-Angle-Spinning Solid-State NMR.',
            authors: 'Shenhui Li and Mei Hong.',
            journal: 'Journal of the American Chemical Society 2011 133 (5), 1534-1544',
            doi: '10.1021/ja108943n',
          },
        ],
      },
      {
        title: 'Ile/Leu/Val rotameric state',
        rows: p.ilv_rotamer,
        tooltip: [
          {
            title:
              'Determination of Isoleucine Side-Chain Conformations in Ground and Excited States of Proteins from Chemical Shifts.',
            authors: 'D. Flemming Hansen, Philipp Neudecker, and Lewis E. Kay.',
            journal: 'Journal of the American Chemical Society 2010 132 (22), 7589-7591',
            doi: '10.1021/ja102090z',
          },
          {
            title:
              'Dependence of Amino Acid Side Chain 13C Shifts on Dihedral Angle: Application to Conformational Analysis.',
            authors: 'Robert E. London, Brett D. Wingad, and Geoffrey A. Mueller.',
            journal: 'Journal of the American Chemical Society 2008 130 (33), 11097-11105',
            doi: '10.1021/ja802729t',
          },
        ],
      },
    ].filter((t) => t.rows.length > 0);
  }

  /** Histogram chart panels for one saveframe (0 or 1). */
  chemShiftHistogramPanels(sf: ChemShiftSaveframe): ChartPanel[] {
    return sf.histogram.map((h) => ({
      title: 'Normalized assigned chemical shifts (Z-score)',
      // Reversed X-axis (high → low Z-score) to match ordinary NMR spectra, and
      // [v, v + step) range labels since each bin spans a Z-score interval.
      option: this.histogramOption(h, 'Z-score', '# of chemical shifts', {
        inverse: true,
        rangeLabels: true,
      }),
    }));
  }

  /** RCI / S² / NMR-RMSD per-residue line panels for one saveframe. */
  rciPanelsOf(sf: ChemShiftSaveframe): ChartPanel[] {
    return sf.rci.map((c) => ({
      title: `${c.label} — Entity_assembly_ID: ${c.chain}`,
      option: this.lineOption(c),
    }));
  }

  /** Status badge color (OK/Warning/Error → teal/amber/red). */
  statusColor(status: string | null): string {
    if (status === 'Error') return 'text-red-600 dark:text-red-400';
    if (status === 'Warning') return 'text-amber-600 dark:text-amber-400';
    return 'text-teal-600 dark:text-teal-400';
  }

  /** The sequence-coverage row for a chain within a chem-shift saveframe, so the
   * aligned-sequence block can be shown alongside that chain's completeness. */
  seqCoverageOf(sf: ChemShiftSaveframe, chain: string): SeqCoverageRow | undefined {
    return sf.sequence_coverage.find((s) => s.chain === chain);
  }

  /** Distance restraints grouped by saveframe (sf_framecode). */
  distRestraintSaveframes = computed(() => this.nmrPreview()?.dist_restraint_saveframes ?? []);

  /** Per-saveframe distance chart panels (reuse the shared option builders). */
  distHistogramPanels(sf: DistRestraintSaveframe): ChartPanel[] {
    return sf.histogram.map((h) => ({
      title: 'Distance restraint target values',
      option: this.histogramOption(h, 'Target distance (Å)', '# of distance restraints', {
        rangeLabels: true,
        yAxisLine: true,
      }),
    }));
  }
  distDiscrepancyPanels(sf: DistRestraintSaveframe): ChartPanel[] {
    return sf.discrepancy.map((h) => ({
      title: 'Discrepancy in redundant distance restraints',
      option: this.histogramOption(h, 'Normalized discrepancy (%)', '# of redundant restraints', {
        rangeLabels: true,
        yAxisLine: true,
      }),
    }));
  }
  distPerResiduePanels(sf: DistRestraintSaveframe): ChartPanel[] {
    return sf.per_residue.map((c) => ({
      title: `Distance restraints per residue — Entity_assembly_ID: ${c.chain}`,
      option: this.perResidueOption(c),
    }));
  }
  distContactMapPanels(sf: DistRestraintSaveframe): ChartPanel[] {
    return sf.contact_maps.map((c) => ({
      title: `Distance restraints contact map — Entity_assembly_ID: ${c.chain}`,
      option: this.contactMapOption(c),
      // Square residue × residue plot; marginX reserves the right-side legend
      // (left 48 + legend width) so the box widens rather than squashing the plot.
      aspect: 1,
      marginX: 48 + this.legendReserve(c.series.map((s) => s.name)),
      marginY: 32,
    }));
  }
  distAsymContactMapPanels(sf: DistRestraintSaveframe): ChartPanel[] {
    return sf.asym_contact_maps.map((c) => {
      const xr = c.xmax - c.xmin;
      const yr = c.ymax - c.ymin;
      return {
        title: `Inter-chain contact map — Entity_assembly_IDs: ${c.chain1} ↔ ${c.chain2}`,
        option: this.asymContactMapOption(c),
        // Proportional to the two chains' residue ranges (undistorted cells);
        // margins reserve the right-side legend so the plot keeps that ratio.
        aspect: xr > 0 ? yr / xr : 1,
        marginX: 48 + this.legendReserve(c.series.map((s) => s.name)),
        // top 16 + bottom 40 + x-tick labels; matches the asym grid so the plot
        // stays proportional and the x-axis title has room.
        marginY: 72,
      };
    });
  }

  /** Dihedral angle restraints grouped by saveframe (sf_framecode). */
  dihedRestraintSaveframes = computed(() => this.nmrPreview()?.dihed_restraint_saveframes ?? []);

  /** Per-saveframe dihedral chart panels (reuse the shared option builders). */
  dihedScatterPanels(sf: DihedRestraintSaveframe): ChartPanel[] {
    const panels: ChartPanel[] = [];
    for (const d of sf.dihedral) {
      if (d.phi_psi)
        panels.push({
          // Square: both axes span -180..180°, so the plot must stay rectangular.
          title: 'φ / ψ dihedral angles',
          option: this.dihedralOption(d.phi_psi, 'φ', 'ψ'),
          aspect: 1,
        });
      if (d.chi1_chi2)
        panels.push({
          title: 'χ1 / χ2 dihedral angles',
          option: this.dihedralOption(d.chi1_chi2, 'χ1', 'χ2'),
          aspect: 1,
        });
    }
    return panels;
  }
  dihedHistogramPanels(sf: DihedRestraintSaveframe): ChartPanel[] {
    return sf.histogram.map((h) => ({
      title: 'Dihedral angle target values',
      option: this.histogramOption(h, 'Angle (°)', '# of dihedral angle restraints', {
        rangeLabels: true,
        yAxisLine: true,
      }),
    }));
  }
  dihedDiscrepancyPanels(sf: DihedRestraintSaveframe): ChartPanel[] {
    return sf.discrepancy.map((h) => ({
      title: 'Discrepancy in redundant dihedral angle restraints',
      option: this.histogramOption(
        h,
        'Discrepancy in dihedral angle restraints (°)',
        '# of redundant restraints',
        { rangeLabels: true, yAxisLine: true },
      ),
    }));
  }
  dihedPerResiduePanels(sf: DihedRestraintSaveframe): ChartPanel[] {
    return sf.per_residue.map((c) => ({
      title: `Dihedral angles per residue — Entity_assembly_ID: ${c.chain}`,
      option: this.lineOption(c),
    }));
  }

  /** RDC restraints grouped by saveframe (sf_framecode). */
  rdcRestraintSaveframes = computed(() => this.nmrPreview()?.rdc_restraint_saveframes ?? []);

  /** Per-saveframe RDC chart panels (reuse the shared option builders). */
  rdcHistogramPanels(sf: RdcRestraintSaveframe): ChartPanel[] {
    return sf.histogram.map((h) => ({
      title: 'Observed RDC values',
      option: this.histogramOption(h, 'Obs. RDC value (Hz)', '# of RDC restraints', {
        rangeLabels: true,
        yAxisLine: true,
      }),
    }));
  }
  rdcDiscrepancyPanels(sf: RdcRestraintSaveframe): ChartPanel[] {
    return sf.discrepancy.map((h) => ({
      title: 'Discrepancy in redundant RDC restraints',
      option: this.histogramOption(
        h,
        'Discrepancy in RDC restraints (Hz)',
        '# of redundant restraints',
        { rangeLabels: true, yAxisLine: true },
      ),
    }));
  }
  rdcPerResiduePanelsOf(sf: RdcRestraintSaveframe): ChartPanel[] {
    return sf.per_residue.map((c) => ({
      title: `Observed RDC per residue — Entity_assembly_ID: ${c.chain}`,
      option: this.lineOption(c),
    }));
  }

  /** True when the preview has any chart or table content to show. */
  hasPreviewContent = computed(
    () =>
      this.previewInventory().length > 0 ||
      this.assemblyProps().length > 0 ||
      this.chemShiftSaveframes().length > 0 ||
      this.distRestraintSaveframes().length > 0 ||
      this.dihedRestraintSaveframes().length > 0 ||
      this.rdcRestraintSaveframes().length > 0 ||
      this.spectralPeakSaveframes().length > 0 ||
      this.previewAlignments().length > 0 ||
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

  /** The conversion results have been downloaded (drives the read-only notice). */
  downloaded = computed(() => this.pageService.pageState().downloaded);

  /** A conversion run exists (the validation/approval UI only applies then). */
  processed = computed(() => this.pageService.pageState().conversionId !== null);

  /** Whether the "Proceed to download" button is shown (Terms #7). Hidden in the
   * Error case (blocking NMR error → must re-upload) and while the NMR data
   * report is unavailable/still loading. In the Warning case it shows but is only
   * enabled once every issue is acknowledged (canApprove); in the OK case there
   * is nothing to acknowledge, so it is enabled immediately. */
  showProceed = computed(
    () => this.processed() && this.nmrAvailable() === true && !this.hasBlockingError(),
  );

  /** Warning status: the button is shown but there are issues to acknowledge
   * first (in the OK case there are none). */
  warningStatus = computed(() => this.showProceed() && this.acknowledgeableKeys().length > 0);

  /** Navigate to the download page once the status is approved (Terms #7). */
  proceedToDownload(): void {
    if (!this.canApprove()) return;
    this.router.navigate(['/download'], { queryParamsHandling: 'preserve' });
  }

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
    this.http.get<{ files: UploadFileRow[] }>(API_URL + 'files', { params: { token } }).subscribe({
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
    this.http.get<NmrPreview>(API_URL + 'nmr_preview', { params: { token } }).subscribe({
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
  private histogramOption(
    h: HistogramChart,
    xName: string,
    yName: string,
    opts: { inverse?: boolean; rangeLabels?: boolean; yAxisLine?: boolean } = {},
  ): object {
    const { inverse = false, rangeLabels = false, yAxisLine = false } = opts;
    // Categories are bin lower bounds; the bin spans [v, v + step). When
    // rangeLabels is set, label each tick with that half-open interval so the
    // axis reads as ranges rather than single points.
    const step =
      h.categories.length >= 2 ? parseFloat(h.categories[1]) - parseFloat(h.categories[0]) : 0;
    const labelFormatter =
      rangeLabels && step && inverse
        ? (value: string) => `(${+(parseFloat(value) + step).toFixed(6)}, ${value}]`
        : rangeLabels && step
          ? (value: string) => `[${value}, ${+(parseFloat(value) + step).toFixed(6)})`
          : undefined;
    // Outlier markers: a dashed vertical line at each annotated value's precise
    // position, red for anomalous shifts (else slate), with a rotated short
    // description. `a.x` is a fractional category index; the category axis snaps
    // markLines to bin centres, so the markers are anchored to a hidden value
    // axis (index 1) that spans the same pixel extent — value i lands on band
    // centre i, letting fractional positions render exactly between bins.
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
              // Anchor the rotated text at its start so it hangs downward from
              // the top of the line (default centering clipped the leading
              // residue id above the plot, leaving only value / Z score).
              position: 'end',
              rotate: -90,
              align: 'left',
              // 'bottom' shifts the rotated text column to the right of the
              // dashed line so the text and line no longer overlap.
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
      // Reversed (high → low) to match the convention of NMR spectra.
      inverse,
    };
    // Hidden value axis matching the category axis extent (bands span index
    // -0.5 … n-0.5); markLine values are fractional indices placed against it.
    const markerAxis = {
      type: 'value',
      min: 0.0,
      max: n + 1.0,
      show: false,
      // inverse,
      axisPointer: { show: false },
    };
    return {
      title: { text: h.label, left: 'center', textStyle: { fontSize: 12, fontWeight: 'normal' } },
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
      // Exclude the invisible marker-holder series from the legend.
      legend: { bottom: 0, type: 'scroll', data: h.series.map((s) => s.name) },
      grid: { left: 56, right: 16, top: 36, bottom: 64, containLabel: true },
      xAxis: markLine ? [categoryAxis, markerAxis] : categoryAxis,
      // A value axis hides its axis line by default; show it for the restraint
      // histograms (skipped for the chem-shift chart). Tick marks are shown on
      // every histogram's y-axis.
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

  /** ECharts option for a dihedral scatter with custom bidirectional error bars
   * (a `custom` series — no third-party plugin). */
  private dihedralOption(plot: DihedralPlot, xName: string, yName: string): object {
    const axis = (name: string, nameGap: number) => ({
      type: 'value' as const,
      name,
      // Centre the title along each axis (bottom-centre / left-centre) so it never
      // collides with the right-side legend. onZero:false keeps the axis (and its
      // title) at the grid edge rather than the centre 0-crossing; the ±180° grid
      // is still conveyed by the split lines.
      nameLocation: 'middle' as const,
      nameGap,
      min: -180,
      max: 180,
      interval: 90,
      splitLine: { show: true },
      axisLine: { show: false, onZero: false },
      axisTick: { show: false },
    });
    return {
      tooltip: {
        trigger: 'item',
        // seriesName is the comp_id (residue type), data.name is the seq_id.
        formatter: (p: {
          seriesName?: string;
          data?: { name?: string | number; value?: number[] };
        }) =>
          p.data?.value
            ? `${p.data.name} ${p.seriesName}<br/>${xName}: ${p.data.value[0]}°<br/>${yName}: ${p.data.value[1]}°`
            : '',
      },
      // Legend of residue types (comp_id); plain so the many entries wrap onto
      // multiple lines. Excludes the invisible error series.
      // Residue-type legend on the right (comp_id entries are short).
      legend: {
        orient: 'vertical',
        right: 8,
        top: 'middle',
        type: 'plain',
        data: plot.groups.map((g) => g.comp_id),
      },
      grid: { left: 56, right: 76, top: 24, bottom: 24, containLabel: true },
      xAxis: axis(`${xName} (°)`, 28),
      yAxis: axis(`${yName} (°)`, 40),
      series: [
        // One scatter series per residue type (comp_id) → categorized legend.
        // Listed first so each gets a consecutive palette colour and provides the
        // legend icon colour.
        ...plot.groups.map((g) => ({
          name: g.comp_id,
          type: 'scatter',
          z: 2,
          symbolSize: 6,
          itemStyle: { opacity: 0.7 },
          data: g.points.map((pt) => ({ name: pt.seq_id, value: [pt.x, pt.y] })),
        })),
        // Matching error-bar series per comp_id, drawn beneath the points. Sharing
        // the comp_id name makes the legend toggle show/hide bars with the points.
        ...plot.groups.map((g) => ({
          name: g.comp_id,
          type: 'custom',
          silent: true,
          z: 1,
          data: g.errors,
          encode: { x: 0, y: 1 },
          renderItem: this.errorBarRenderItem,
        })),
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

  /** More saturated band color used as a thin edge line on the band's sides. */
  private bandEdgeColor(type: string): string {
    if (type === 'helix') return 'rgba(204,47,0,0.55)';
    if (type === 'strand') return 'rgba(0,156,209,0.55)';
    if (type === 'turn') return 'rgba(200,204,0,0.65)';
    return 'rgba(120,120,120,0.4)';
  }

  /** Secondary-structure bands as a markArea overlay (shared by the per-residue
   * bar and line charts). A markArea attached to a category axis snaps to bin
   * centres for a line series but to bin edges for a bar series — inconsistent,
   * and centre-anchored bands stop at the middle of the border bins. Instead
   * anchor it to a hidden value axis (xAxisIndex 1) whose value v maps to the
   * category fraction v/n: value i sits on bin i's left edge, so [start, end + 1]
   * covers the full bins of the band's first and last residues. Returns the
   * hidden axis and the invisible line series that carries the markArea; splice
   * both into a chart whose primary (category) data sits at xAxisIndex 0. */
  private bandOverlay(
    categories: string[],
    bands: { start: number; end: number; type: string; label: string }[],
  ): { markerAxis: object; holderSeries: object } {
    const markArea = {
      silent: true,
      // Secondary-structure word (struct_conf, e.g. HELX_P / STRN) annotated at
      // each band's top-left corner, rotated to read top-to-bottom so the
      // (often narrow) band can carry the label.
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
          // Translucent fill with a thin, more saturated border so the band's
          // left/right edges read as condensed colored lines (the band spans the
          // full plot height, so the top/bottom borders sit at the frame edges).
          itemStyle: {
            color: this.bandColor(b.type),
            borderColor: this.bandEdgeColor(b.type),
            borderWidth: 1,
          },
          name: b.label,
        },
        { xAxis: b.end + 1 },
      ]),
    };
    // Hidden value axis matching the category axis pixel extent (n bins over the
    // grid): value i lands on bin i's left edge, value n on the last bin's right
    // edge, so fractional band bounds render exactly against it.
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

  /** ECharts option for a per-residue stacked-count bar with secondary-structure
   * bands drawn as markAreas. */
  private perResidueOption(c: PerResidueChart): object {
    const { markerAxis, holderSeries } = this.bandOverlay(c.categories, c.bands);
    const interval = Math.max(0, Math.ceil(c.categories.length / 24) - 1);
    return {
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
      // Exclude the invisible band-holder line series from the legend.
      legend: { bottom: 0, type: 'scroll', data: c.series.map((s) => s.name) },
      grid: { left: 48, right: 16, top: 24, bottom: 72, containLabel: true },
      xAxis: [
        { type: 'category', data: c.categories, axisLabel: { interval, rotate: -75, fontSize: 8 } },
        markerAxis,
      ],
      yAxis: { type: 'value', name: '# of restraints', minInterval: 1, axisLine: { show: true } },
      series: [
        ...c.series.map((s) => ({ name: s.name, type: 'bar', stack: 'total', data: s.data })),
        holderSeries,
      ],
    };
  }

  /** Translucent fill for a contact-map secondary-structure band. The low alpha
   * lets vertical (x) and horizontal (y) bands blend where they cross. */
  private mapBandColor(type: string): string {
    if (type === 'helix') return 'rgba(204,47,0,0.08)';
    if (type === 'strand') return 'rgba(0,156,209,0.08)';
    if (type === 'turn') return 'rgba(200,204,0,0.10)';
    return 'rgba(120,120,120,0.06)';
  }

  /** A markArea overlaying secondary-structure regions on a contact map: `xbands`
   * become vertical bands (full height), `ybands` horizontal bands (full width).
   * Bands sit at residue value coordinates, so [start-0.5, end+0.5] covers the
   * whole residues; the translucent fills blend where the two axes' structures
   * coincide. */
  private mapBandMarkArea(xbands: MapBand[], ybands: MapBand[]): object {
    const label = (rotate: number, position: string, offset: number[], type: string) => ({
      show: true,
      position,
      offset,
      rotate,
      fontSize: 9,
      fontStyle: 'italic' as const,
      color: this.bandEdgeColor(type),
    });
    const vertical = xbands.map((b) => [
      {
        xAxis: b.start - 0.5,
        itemStyle: { color: this.mapBandColor(b.type) },
        name: b.label,
        label: label(-90, 'insideTopLeft', [8, -4], b.type),
      },
      { xAxis: b.end + 0.5 },
    ]);
    const horizontal = ybands.map((b) => [
      {
        yAxis: b.start - 0.5,
        itemStyle: { color: this.mapBandColor(b.type) },
        name: b.label,
        label: label(0, 'insideStartTop', [8, 2], b.type),
      },
      { yAxis: b.end + 0.5 },
    ]);
    return { silent: true, data: [...vertical, ...horizontal] };
  }

  /** ECharts option for a symmetric contact map: scatter of [seq1, seq2] points
   * sized by restraint count, on a square residue×residue grid (y inverted). */
  /** Max px width for a contact-map right-side legend before labels truncate,
   * so a very long constraint name (e.g. a hydrogen-bond description) can't eat
   * the plot. */
  private static readonly LEGEND_CAP = 300;

  /** Right-margin (px) to reserve for the vertical legend of `names`, sized to
   * the longest label (≈ 82 + 5.8·chars) and capped at LEGEND_CAP. Used for both
   * grid.right (option) and the plot marginX (panel) so they stay in sync. */
  private legendReserve(names: string[]): number {
    const maxLen = names.reduce((m, n) => Math.max(m, n.length), 0);
    return Math.min(Summary.LEGEND_CAP, Math.round(82 + 5.8 * maxLen));
  }

  /** Vertical right-side legend for the contact maps; labels wider than the cap
   * truncate with an ellipsis (full text shown on legend hover). */
  private mapLegend(): object {
    return {
      orient: 'vertical',
      right: 8,
      top: 'middle',
      type: 'plain',
      textStyle: { width: Summary.LEGEND_CAP - 82, overflow: 'truncate' },
      tooltip: { show: true },
    };
  }

  private contactMapOption(c: ContactMapChart): object {
    const axis = () => ({
      type: 'value' as const,
      min: c.min,
      max: c.max,
      minInterval: 1,
      // No axis line / ticks / title: the residue-range start (residue 0) needs
      // no emphasis, and the bands carry the residue context.
      axisLine: { show: false },
      axisTick: { show: false },
    });
    // Symmetric map: the same sequence bands read on both axes.
    const markArea = this.mapBandMarkArea(c.bands, c.bands);
    return {
      tooltip: {
        trigger: 'item',
        formatter: (p: { data?: ContactPoint }) => {
          const d = p.data;
          return d ? `${d.c1} ${d.value[0]} ↔ ${d.c2} ${d.value[1]}<br/>count: ${d.value[2]}` : '';
        },
      },
      // Constraint-type legend on the right, sized to the longest label.
      legend: this.mapLegend(),
      grid: {
        left: 48,
        right: this.legendReserve(c.series.map((s) => s.name)),
        top: 16,
        bottom: 16,
        containLabel: true,
      },
      xAxis: axis(),
      yAxis: { ...axis(), inverse: true },
      series: c.series.map((s, idx) => ({
        name: s.name,
        type: 'scatter',
        data: s.points,
        symbolSize: (v: number[]) => Math.min(16, 4 + 2 * (v[2] || 1)),
        ...(idx === 0 ? { markArea } : {}),
      })),
    };
  }

  /** ECharts option for an asymmetric (inter-chain) contact map: scatter with
   * independent x (chain 1) and y (chain 2) residue ranges, sized by count. */
  private asymContactMapOption(c: AsymContactMap): object {
    return {
      tooltip: {
        trigger: 'item',
        formatter: (p: { data?: ContactPoint }) => {
          const d = p.data;
          return d
            ? `${c.chain1} ${d.c1} ${d.value[0]} ↔ ${c.chain2} ${d.c2} ${d.value[1]}<br/>count: ${d.value[2]}`
            : '';
        },
      },
      // Constraint-type legend on the right, sized to the longest label.
      legend: this.mapLegend(),
      grid: {
        left: 48,
        right: this.legendReserve(c.series.map((s) => s.name)),
        top: 16,
        // Extra bottom room so the centred x-axis title isn't clipped (matched
        // by marginY on the panel so the plot keeps its aspect).
        bottom: 40,
        containLabel: true,
      },
      // No axis lines / ticks: the residue-range start (residue 0)
      // needs no emphasis, and the bands carry the residue context.
      xAxis: {
        type: 'value',
        name: `Entity_assembly_ID: ${c.chain1}`,
        // Centre the title under the axis (was at the right end).
        nameLocation: 'middle',
        nameGap: 28,
        min: c.xmin,
        max: c.xmax,
        minInterval: 1,
        axisLine: { show: false },
        axisTick: { show: false },
      },
      yAxis: {
        type: 'value',
        name: `Entity_assembly_ID: ${c.chain2}`,
        // Rotate the title vertically along the axis so it doesn't run into the
        // element above the chart.
        nameLocation: 'middle',
        nameRotate: 90,
        nameGap: 44,
        min: c.ymin,
        max: c.ymax,
        minInterval: 1,
        axisLine: { show: false },
        axisTick: { show: false },
      },
      series: c.series.map((s, idx) => ({
        name: s.name,
        type: 'scatter',
        data: s.points,
        symbolSize: (v: number[]) => Math.min(16, 4 + 2 * (v[2] || 1)),
        ...(idx === 0 ? { markArea: this.mapBandMarkArea(c.xbands, c.ybands) } : {}),
      })),
    };
  }

  /** ECharts option for a per-residue value line chart (dihedral / RDC / RCI),
   * with secondary-structure bands and an optional threshold line. */
  private lineOption(c: PerResidueLine): object {
    const interval = Math.max(0, Math.ceil(c.categories.length / 24) - 1);
    const { markerAxis, holderSeries } = this.bandOverlay(c.categories, c.bands);
    const markLine =
      c.threshold !== null
        ? {
            silent: true,
            symbol: 'none',
            // Label anchored at the left, inside the grid, so the descriptive
            // text isn't clipped (the default end position showed only "0.").
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
      // Exclude the invisible band-holder line series from the legend.
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
          // Show point symbols so sparse series (e.g. RCI/S², defined only for some
          // residues) remain visible: an isolated value surrounded by nulls draws
          // no line segment, so without a symbol it would render as nothing.
          showSymbol: true,
          symbolSize: 4,
          ...(idx === 0 && markLine ? { markLine } : {}),
        })),
        holderSeries,
      ],
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
    this.http.post(API_URL + 'approve', { token, approved: value }).subscribe({
      next: () => this.pageService.pageState.update((prev) => ({ ...prev, approved: value })),
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
      // Rejects after exhausting retries → caught below to show the fallback.
      await this.loadCoordinateWithRetry(viewer, url);
    } catch (err) {
      console.error('Mol* coordinate preview unavailable', err);
      this.viewerError.set(true);
      this.disposeViewer();
    }
  }

  /**
   * Load the converted coordinate into the viewer, retrying transient failures.
   * On arrival straight from the processing dialog, /api/progress can report the
   * run "done" a moment before the backend harvests the coordinate output_file
   * row, so /api/coordinate briefly 404s. Retry a few times (~1s apart) so the
   * preview appears on its own instead of requiring a manual page refresh.
   */
  private async loadCoordinateWithRetry(viewer: MolstarViewer, url: string): Promise<void> {
    const attempts = 6;
    const delayMs = 1000;
    for (let i = 0; ; i++) {
      // Bail out quietly if the viewer was torn down (component destroyed) mid-retry.
      if (this.viewer !== viewer) return;
      try {
        await viewer.loadStructureFromUrl(url, 'mmcif', false);
        return;
      } catch (err) {
        if (i >= attempts - 1) throw err;
        await new Promise((resolve) => setTimeout(resolve, delayMs));
      }
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
