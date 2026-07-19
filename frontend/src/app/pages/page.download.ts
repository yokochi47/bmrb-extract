import { Component, DestroyRef, computed, effect, inject, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { HttpClient } from '@angular/common/http';
import { RouterLink } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { CardModule } from 'primeng/card';
import { TableModule } from 'primeng/table';
import { ButtonModule } from 'primeng/button';
import { MessageModule } from 'primeng/message';
import { PanelModule } from 'primeng/panel';
import { timer } from 'rxjs';
import { switchMap, takeWhile } from 'rxjs/operators';

import { PageService, TargetDepsys } from './page.service';
import { API_URL, HOST_SITE_URL } from '../../site.config';
import { fileTypeLabel } from './file-types';
import { EchartComponent } from './echart.component';

/** One conversion-result file bundled in the download zip. */
interface OutputFileRow {
  name: string;
  file_type: string;
  file_size: number;
}

/** A selected upload file participating in the conversion (GET /api/files). */
interface UploadFileRow {
  original_name: string;
  file_size: number;
  file_type: string;
  source: string;
  /** Upload time as a naive UTC string ("YYYY-MM-DD HH:mm"); see GET /api/files. */
  uploaded_at: string | null;
}

// ── Conversion statistics (GET /api/output_statistics) ─────────────────────────
// All fields optional: the report only partially populates output_statistics, so
// the UI renders defensively and omits anything missing.
/** A two-column Property/Value row for the key-value cards. */
interface KVRow {
  label: string;
  value: string;
}
interface StatModel {
  file_name?: string;
  file_type?: string;
  struct_title?: string;
  audit_authors?: string;
  file_size?: number;
  md5_checksum?: string;
}
interface StatSoftware {
  name?: string;
  version?: string;
  classification?: string;
}
interface StatEntityAssembly {
  entity_assembly_id?: number;
  entity_assembly_name?: string;
  entity_id?: number;
  entity_label?: string;
  chain_id?: string;
  auth_chain_id?: string;
  experimental_data_reported?: boolean;
  physical_state?: string;
  role?: string;
}
interface StatAssembly {
  name?: string;
  number_of_components?: number;
  organic_ligands?: number;
  metal_ions?: number;
  non_standard_bonds?: boolean;
  paramagnetic?: boolean;
  thiol_state?: string;
  molecular_mass?: number;
  entity_assembly?: StatEntityAssembly[];
}
interface StatEntity {
  entity_id?: number;
  label?: string;
  name?: string;
  type?: string;
  polymer_common_type?: string;
  polymer_type?: string;
  polymer_seq_one_letter_code?: string;
  auth_chain_id?: string[];
  number_of_monomers?: number;
  number_of_nonpolymer_components?: number;
  nstd_monomer?: boolean;
  nstd_linkage?: boolean;
  paramagnetic?: boolean;
  thiol_state?: string;
  formula_weight?: number;
}
/** Overall assigned-chemical-shift completeness (output_statistics.chem_shift_summary).
 * completeness_* are fractions (0–1) shown as percent. */
interface StatChemShiftSummary {
  number_of_target_shifts_in_well_defined_region?: number;
  number_of_assigned_shifts_in_well_defined_region?: number;
  number_of_favorable_assigned_shifts_in_well_defined_region?: number;
  completeness_in_well_defined_region?: number;
  completeness_in_well_defined_region_with_favorable_shift?: number;
  number_of_target_shifts_in_full_length_region?: number;
  number_of_assigned_shifts_in_full_length_region?: number;
  number_of_favorable_assigned_shifts_in_full_length_region?: number;
  completeness_in_full_length_region?: number;
  completeness_in_full_length_region_with_favorable_shift?: number;
}
/** One unmapped assigned chemical shift (chem_shift[].chemical_shift_unmapped). */
interface StatChemShiftUnmapped {
  auth_chain_id?: string;
  auth_seq_id?: number;
  ins_code?: string | null;
  comp_id?: string;
  atom_id?: string;
  value?: number;
  error?: number | null;
  ambig_code?: number | null;
}
/** One unparsed chemical shift (chem_shift[].chemical_shift_unparsed); value/error/
 * ambig_code may be raw strings since the shift could not be parsed. */
interface StatChemShiftUnparsed {
  auth_chain_id?: string;
  auth_seq_id?: number;
  ins_code?: string | null;
  comp_id?: string;
  atom_id?: string;
  value?: number | string | null;
  error?: number | string | null;
  ambig_code?: number | string | null;
}
/** One chemical shift outlier (chem_shift[].chemical_shift_outlier). */
interface StatChemShiftOutlier {
  auth_chain_id?: string;
  auth_seq_id?: number;
  ins_code?: string | null;
  comp_id?: string;
  atom_id?: string;
  value?: number;
  ambig_code?: number | null;
  z_score?: number;
  expected_range?: { min_value?: number; max_value?: number };
  /** Structural factor behind the outlier (free text), or null when none. */
  details?: string | null;
}
/** One completeness entry (one nucleus/atom_group within an assignment category). */
interface StatCompletenessEntry {
  atom_group?: string;
  number_of_assigned_shifts?: number;
  number_of_target_shifts?: number;
  completeness?: number | null;
}
/** completeness_in_*_region: assignment-category arrays. Schema key spellings are
 * preserved verbatim (several carry typos in the report generator). */
interface StatCompletenessRegion {
  completeness_of_overall_assignments?: StatCompletenessEntry[];
  completeness_of_favorable_assignments?: StatCompletenessEntry[];
  completeness_of_backbone_assignments?: StatCompletenessEntry[];
  completeness_of_sidechain_assignments?: StatCompletenessEntry[];
  completeness_of_aromatic_assignments?: StatCompletenessEntry[];
  completeness_of_sugar_assignments?: StatCompletenessEntry[];
  completeness_of_base_assignments?: StatCompletenessEntry[];
  completeness_of_stereomethyl_assignments?: StatCompletenessEntry[];
}
/** One assigned-chemical-shift histogram (chem_shift[].histogram), pre-shaped by
 * the backend into ECharts-ready categories/series (mirrors the summary page). */
interface HistogramChart {
  label: string;
  categories: string[];
  series: { name: string; data: number[] }[];
  /** Outlier markers (chem-shift Z scores): dashed line + short description.
   * `x` is the precise fractional category-axis index of the value. */
  annotations?: { x: number; anomalous: boolean; text: string }[];
}
/** Per-residue (Comp_ID) author→CCD atom-name mapping history; `unusual` flags an
 * unexpected mapping (rendered red). Same shape as the summary page. */
interface AtomNameMappingRow {
  comp_id: string;
  history: { name: string; atoms: string; unusual: boolean }[];
}
/** One restraint / spectral-peak saveframe's common bookkeeping (+ atom-name
 * mapping); shares output_stats_common_bookkeeping semantics with chem_shift. */
interface RestraintBookkeepingSaveframe {
  original_file_name?: string | null;
  list_id?: number;
  sf_framecode?: string;
  number_of_parsed?: number;
  number_of_mapped_to_model?: number;
  number_of_unmapped_to_model?: number;
  number_of_unparsed_with_error?: number;
  number_of_parsed_with_warning?: number;
  atom_name_mapping?: AtomNameMappingRow[];
}
/** Per-residue line chart (RCI/S² or NMR RMSD) with structural bands, keyed by
 * the coordinate residue scheme (auth_chain_id/auth_seq_id) on the download page. */
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
/** Per-saveframe assigned-chemical-shift bookkeeping (output_statistics.chem_shift);
 * the backend prunes each item to these counts (see _CHEM_SHIFT_STATS_KEYS). */
interface StatChemShiftSaveframe {
  original_file_name?: string | null;
  list_id?: number;
  sf_framecode?: string;
  number_of_parsed?: number;
  number_of_mapped_to_model?: number;
  number_of_unmapped_to_model?: number;
  number_of_unparsed_with_error?: number;
  number_of_parsed_with_warning?: number;
  number_of_outliers?: number;
  chemical_shift_unmapped?: StatChemShiftUnmapped[];
  chemical_shift_outlier?: StatChemShiftOutlier[];
  chemical_shift_unparsed?: StatChemShiftUnparsed[];
  /** Duplicated shifts share the unmapped column shape (value/error/ambig_code). */
  chemical_shift_duplicated?: StatChemShiftUnmapped[];
  completeness_in_well_defined_region?: StatCompletenessRegion;
  completeness_in_full_length_region?: StatCompletenessRegion;
  atom_name_mapping?: AtomNameMappingRow[];
  histogram?: HistogramChart[];
  rci?: PerResidueLine[];
}

/** One well-defined region of the coordinate ensemble (ensemble_composition). */
interface StatEnsembleRegion {
  domain_id?: number;
  medoid_model_id?: number;
  number_of_monomers?: number;
  percent_of_core?: number;
  medoid_rmsd?: number;
  range_of_seq_id?: string;
}
/** One cluster of the coordinate ensemble (cluster_id === -1 → single-model). */
interface StatEnsembleCluster {
  cluster_id?: number;
  model_ids?: number[];
  centroid_model_id?: number;
  mean_rmsd?: number;
  /** Per-model PC1/PC2 coordinates for the PCA scatter. */
  principal_components?: { model_id?: number; pc1?: number; pc2?: number }[];
}
/** Coordinate ensemble composition (input_sources[file_type='pdbx']). */
interface StatEnsembleComposition {
  total_models?: number;
  representative_model_id?: number;
  selection_criteria?: string | null;
  well_defined_region?: StatEnsembleRegion[];
  cluster_analysis?: StatEnsembleCluster[];
}

/** Pivoted completeness table (rows = assignment categories, columns = nuclei)
 * plus the caption's overall / stereomethyl figures. */
interface CompletenessView {
  columns: string[];
  rows: { label: string; cells: string[] }[];
  overallPct: number | null;
  overallAssigned: number | null;
  overallTarget: number | null;
  stereo: { assigned: number; target: number } | null;
}
/** Assignment categories shown as table rows, in display order (schema spellings). */
const COMPLETENESS_ROWS: { key: keyof StatCompletenessRegion; label: string }[] = [
  { key: 'completeness_of_backbone_assignments', label: 'Backbone' },
  { key: 'completeness_of_sidechain_assignments', label: 'Sidechain' },
  { key: 'completeness_of_aromatic_assignments', label: 'Aromatic' },
  { key: 'completeness_of_sugar_assignments', label: 'Sugar' },
  { key: 'completeness_of_base_assignments', label: 'Base' },
  { key: 'completeness_of_overall_assignments', label: 'Overall' },
];
/** Column (nucleus) order. atom_group values encode the nucleus as an isotope
 * token (1h / 13c / 15n / 31p); anything without one is the "all" Total column. */
const NUCLEUS_COLUMNS = ['Total', '¹H', '¹³C', '¹⁵N', '³¹P'];

/** Classify an atom_group string (e.g. "backbone_1h_chemical_shifts",
 * "overall_all_chemical_shifts") into its nucleus column. */
function nucleusColumn(atomGroup: string): string {
  const g = atomGroup.toLowerCase();
  if (g.includes('1h')) return '¹H';
  if (g.includes('13c')) return '¹³C';
  if (g.includes('15n')) return '¹⁵N';
  if (g.includes('31p')) return '³¹P';
  return 'Total';
}

/** Format one completeness cell as "{assigned}/{target} ({pct}%)", or an en-dash
 * placeholder when the (category, nucleus) pair has no entry. */
function fmtCompletenessCell(e?: StatCompletenessEntry): string {
  if (!e) return '– / –';
  const a = e.number_of_assigned_shifts ?? 0;
  const t = e.number_of_target_shifts ?? 0;
  const pct =
    e.completeness != null
      ? Math.round(e.completeness * 100)
      : t
        ? Math.round((a / t) * 100)
        : null;
  return pct == null ? `${a}/${t}` : `${a}/${t} (${pct}%)`;
}

/** Group a category's entries by nucleus column (first entry per column wins). */
function byNucleus(entries?: StatCompletenessEntry[]): Map<string, StatCompletenessEntry> {
  const map = new Map<string, StatCompletenessEntry>();
  for (const e of entries ?? []) {
    const col = nucleusColumn(e.atom_group ?? '');
    if (!map.has(col)) map.set(col, e);
  }
  return map;
}

/** Pivot a completeness-region object into a CompletenessView, or null when empty. */
function buildCompletenessView(region?: StatCompletenessRegion): CompletenessView | null {
  if (!region) return null;
  const present = new Set<string>();
  for (const { key } of COMPLETENESS_ROWS) {
    for (const e of region[key] ?? []) {
      if (e.atom_group) present.add(nucleusColumn(e.atom_group));
    }
  }
  if (!present.size) return null;
  const columns = NUCLEUS_COLUMNS.filter((n) => present.has(n));
  const rows = COMPLETENESS_ROWS.flatMap(({ key, label }) => {
    const arr = region[key];
    if (!arr || !arr.length) return [];
    const byGroup = byNucleus(arr);
    return [{ label, cells: columns.map((c) => fmtCompletenessCell(byGroup.get(c))) }];
  });
  if (!rows.length) return null;
  const overall = byNucleus(region.completeness_of_overall_assignments).get('Total');
  const stereoByNuc = byNucleus(region.completeness_of_stereomethyl_assignments);
  const stereo = stereoByNuc.get('Total') ?? region.completeness_of_stereomethyl_assignments?.[0];
  return {
    columns,
    rows,
    overallPct: overall?.completeness != null ? Math.round(overall.completeness * 100) : null,
    overallAssigned: overall?.number_of_assigned_shifts ?? null,
    overallTarget: overall?.number_of_target_shifts ?? null,
    stereo: stereo
      ? {
          assigned: stereo.number_of_assigned_shifts ?? 0,
          target: stereo.number_of_target_shifts ?? 0,
        }
      : null,
  };
}
interface OutputStatistics {
  file_name?: string;
  file_type?: string;
  entry_id?: string;
  entry_title?: string;
  entry_authors?: string | null;
  submission_date?: string | null;
  processed_date?: string | null;
  processed_site?: string;
  file_size?: number;
  md5_checksum?: string;
  model?: StatModel | null;
  software?: StatSoftware[];
  assembly?: StatAssembly;
  entity?: StatEntity[];
  chem_shift_summary?: StatChemShiftSummary;
  chem_shift?: StatChemShiftSaveframe[];
  /** Overall NMR restraint counts. The backend already drops the average/violation
   * tables; values here are scalar counts keyed by (varied) report field names. */
  restraint_summary?: Record<string, unknown>;
}

/** One violation-size bin (small/medium/large) of the average distance/dihedral
 * violation tables (restraint_summary.average_number_of_*_violations_per_model). */
interface ViolationBin {
  bin_type?: string;
  average_number_of_violations_per_model?: number | null;
  max_violation_in_bin?: number | null;
}
/** One category row of restraint_summary.{dist,dihed}_violation_summary. */
interface ViolationSummaryRow {
  restraint_type?: string;
  restraint_count?: number;
  restraint_percent?: number;
  viol_count?: number;
  viol_inline_percent?: number | null;
  viol_absol_percent?: number;
  consist_viol_count?: number;
  consist_viol_inline_percent?: number | null;
  consist_viol_absol_percent?: number;
}
/** One most-violated restraint (restraint_summary.most_violated_*_restraints). */
interface MostViolatedRow {
  restraint_key?: string;
  distance_type?: string;
  dihedral_angle_name?: string;
  atom_key_1?: string;
  atom_key_2?: string;
  atom_key_3?: string;
  atom_key_4?: string;
  total_violated_models?: number;
  mean_violation?: number | null;
  std_violation?: number | null;
  median_violation?: number | null;
}

/** Display order of the restraint_summary key-value rows. Keys not listed here
 * are appended in their original order. */
const RESTRAINT_KEY_ORDER: string[] = [
  'total_distance_restraints',
  'intra-residue',
  'sequential',
  'medium_range',
  'long_range',
  'inter-chain',
  'hydrogen_bond_restraints',
  'disulfide_bond_restraints',
  'diselenide_bond_restraints',
  'metal_coordination_restraints',
  'total_dihedral_angle_restraints',
  'total_rdc_restraints',
  'number_of_unmapped_restraints',
  'number_of_restaints_per_residue',
  'number_of_long_range_restraints_per_residue',
];

/** Restraint keys whose display label carries a sequence-separation formula.
 * Rendered as HTML (via [innerHTML]) in the NMR restraint validation table. */
const RESTRAINT_LABEL_HTML: Record<string, string> = {
  'intra-residue': 'Intra-residue (<em>| i - j | = 0</em>)',
  sequential: 'Sequential (<em>| i - j | = 1</em>)',
  medium_range: 'Medium range (<em>1 &lt; | i - j | &lt; 5</em>)',
  long_range: 'Long range (<em>| i - j | ≥ 5</em>)',
};

/** Distance-type codes used in restraint_summary *_dist_types (comma-separated). */
const DIST_TYPE_LABELS: Record<string, string> = {
  ir: 'Intra-residue',
  se: 'Sequential',
  mr: 'Medium range',
  lr: 'Long range',
  ic: 'Inter-chain',
};

/** Human-readable labels for the OutputFileType values (GET /api/output_files). */
const OUTPUT_TYPE_LABELS: Record<string, string> = {
  pdbx: 'Coordinates (PDBx/mmCIF)',
  'nmr-star': 'NMR data (NMR-STAR)',
  nef: 'NMR data (NEF)',
  text_report: 'Conversion report (text)',
  json_report: 'Conversion report (JSON)',
  pdf_report: 'Conversion report (PDF)',
  compressed: 'Archive',
};

/**
 * Download page (Terms #7) — see the "Download" UI mockup: a tokenized resume
 * URL with its expiry date, an optional (verified) email of that URL, and the
 * conversion-results zip with a table of its file contents.
 */
@Component({
  selector: 'app-download',
  imports: [
    RouterLink,
    FormsModule,
    CardModule,
    TableModule,
    ButtonModule,
    MessageModule,
    PanelModule,
    EchartComponent,
  ],
  templateUrl: './page.download.html',
})
export class Download {
  private pageService = inject(PageService);
  private http = inject(HttpClient);
  private destroyRef = inject(DestroyRef);

  constructor() {
    // Load the result-file listing once the session token is known, then keep
    // polling while the deferred NEF release is still generating (so the .nef row
    // appears on its own); stops as soon as nef_generating clears.
    let started = false;
    effect(() => {
      const token = this.pageService.pageState().tokenBase;
      if (!token || started) return;
      started = true;
      timer(0, 5000)
        .pipe(
          switchMap(() =>
            this.http.get<{ files: OutputFileRow[]; nef_generating?: boolean }>(
              API_URL + 'output_files',
              { params: { token } },
            ),
          ),
          takeWhile((res) => !!res.nef_generating, true),
          takeUntilDestroyed(this.destroyRef),
        )
        .subscribe({
          next: (res) => {
            this.files.set(res.files ?? []);
            this.nefGenerating.set(!!res.nef_generating);
          },
          error: (err) => console.error('Failed to load output files', err),
        });
    });

    // Load the conversion statistics once (non-polling); mirrors the summary
    // page's token-gated fetch. statsAvailable stays null until the first reply.
    let statsFetched = false;
    effect(() => {
      const token = this.pageService.pageState().tokenBase;
      if (!token || statsFetched) return;
      statsFetched = true;
      this.loadStatistics(token);
    });

    // Load the upload (input) file listing once, for the provenance card.
    let inputFilesFetched = false;
    effect(() => {
      const token = this.pageService.pageState().tokenBase;
      if (!token || inputFilesFetched) return;
      inputFilesFetched = true;
      this.loadInputFiles(token);
    });
  }

  /** Fetch GET /api/files → the files the user uploaded for this conversion. */
  private loadInputFiles(token: string): void {
    this.http.get<{ files: UploadFileRow[] }>(API_URL + 'files', { params: { token } }).subscribe({
      next: (res) => this.inputFiles.set(res.files ?? []),
      error: (err) => console.error('Failed to load upload files', err),
    });
  }

  /** Fetch GET /api/output_statistics → the pruned output_statistics subtree. */
  private loadStatistics(token: string): void {
    this.http
      .get<{
        available: boolean;
        statistics?: OutputStatistics;
        ensemble_composition?: StatEnsembleComposition;
        report_timestamp?: string;
        restraint_bookkeeping?: Record<string, RestraintBookkeepingSaveframe[]>;
      }>(API_URL + 'output_statistics', {
        params: { token },
      })
      .subscribe({
        next: (res) => {
          this.statistics.set(res.statistics ?? null);
          this.ensemble.set(res.ensemble_composition ?? null);
          this.reportTimestamp.set(res.report_timestamp ?? null);
          this.restraintBookkeeping.set(res.restraint_bookkeeping ?? {});
          this.statsAvailable.set(!!res.available);
        },
        error: (err) => {
          console.error('Failed to load conversion statistics', err);
          this.statsAvailable.set(false);
        },
      });
  }

  /** Conversion result files bundled in the zip (GET /api/output_files). */
  files = signal<OutputFileRow[]>([]);

  /** Files the user uploaded for this conversion (GET /api/files). */
  inputFiles = signal<UploadFileRow[]>([]);
  /** Show the Source column only when a file did not come from the user. */
  showInputSource = computed(() => this.inputFiles().some((f) => f.source !== 'user'));

  /** The deferred NMR-STAR→NEF release is still running (poll until it clears). */
  nefGenerating = signal(false);

  /** A NEF file was produced — else the table shows the NEF-unavailable note. */
  hasNef = computed(() => this.files().some((f) => f.file_type === 'nef'));

  /** Conversion statistics subtree (GET /api/output_statistics); null until
   * loaded or when the report has no output_statistics. */
  statistics = signal<OutputStatistics | null>(null);
  /** Tri-state: null = loading, false = not available, true = show the cards. */
  statsAvailable = signal<boolean | null>(null);
  /** Report file modification time (UTC, "YYYY-MM-DD HH:MM:SS"); null until loaded. */
  reportTimestamp = signal<string | null>(null);
  /** Human-readable deposition target the converted output is intended for. */
  outputUsedFor = computed(() => {
    switch (this.pageService.pageState().targetDepsys) {
      case TargetDepsys.repl_cs:
        return 'OneDep (ongoing deposition - replacing assigned chemical shifts)';
      case TargetDepsys.bmrbdep:
        return 'BMRBdep (new deposition)';
      default:
        return 'OneDep (new deposition)';
    }
  });
  /** Site that processed the conversion — the report's processed_site, falling
   * back to this deployment's host URL. */
  processedSite = computed(() => this.statistics()?.processed_site ?? HOST_SITE_URL);
  /** Restraint / spectral-peak bookkeeping saveframes, keyed by subtype. */
  restraintBookkeeping = signal<Record<string, RestraintBookkeepingSaveframe[]>>({});

  /** Restraint / spectral-peak bookkeeping sections (fixed 6.3–6.6 numbering),
   * each with per-saveframe bookkeeping rows + atom-name-mapping history. */
  restraintBookkeepingGroups = computed(() => {
    const bk = this.restraintBookkeeping();
    const defs = [
      {
        key: 'dist_restraint',
        section: '6.3',
        heading: 'Bookkeeping of distance restraints',
        empty: 'There is no distance restraints.',
        noun: 'distance restraints',
      },
      {
        key: 'dihed_restraint',
        section: '6.4',
        heading: 'Bookkeeping of dihedral-angle restraints',
        empty: 'There is no dihedral-angle restraints.',
        noun: 'dihedral-angle restraints',
      },
      {
        key: 'rdc_restraint',
        section: '6.5',
        heading: 'Bookkeeping of RDC restraints',
        empty: 'There is no RDC restraints.',
        noun: 'RDC restraints',
      },
      {
        key: 'spectral_peak',
        section: '6.6',
        heading: 'Bookkeeping of spectral peak lists',
        empty: 'There is no spectral peak lists.',
        noun: 'spectral peaks',
      },
    ];
    return defs.map((d) => ({
      section: d.section,
      heading: d.heading,
      emptyText: d.empty,
      saveframes: (bk[d.key] ?? []).map((s) => ({
        listId: s.list_id ?? 0,
        title: `${s.sf_framecode} (${s.original_file_name})`,
        // Atom-name mapping is meaningless when nothing mapped to the model.
        mappedToModel: s.number_of_mapped_to_model,
        rows: [
          this.kv(`Number of parsed ${d.noun}`, s.number_of_parsed),
          this.kv(`Number of ${d.noun} mapped to model`, s.number_of_mapped_to_model),
          this.kv(`Number of ${d.noun} unmapped to model`, s.number_of_unmapped_to_model),
          this.kv(`Number of unparsed ${d.noun} with error`, s.number_of_unparsed_with_error),
          this.kv(`Number of parsed ${d.noun} with warning`, s.number_of_parsed_with_warning),
        ].filter((r): r is KVRow => r !== null),
        atomNameMapping: s.atom_name_mapping ?? [],
      })),
    }));
  });

  /** Coordinate ensemble composition (GET /api/output_statistics); null when the
   * report has no pdbx input source with a well-defined-region analysis. */
  ensemble = signal<StatEnsembleComposition | null>(null);
  /** Well-defined region rows for the ensemble-composition table. */
  ensembleRegions = computed<StatEnsembleRegion[]>(
    () => this.ensemble()?.well_defined_region ?? [],
  );
  /** Show the Ensemble composition card only when there are well-defined regions. */
  hasEnsemble = computed(() => this.ensembleRegions().length > 0);
  /** Total model count in the ensemble (caption). */
  ensembleTotalModels = computed(() => this.ensemble()?.total_models ?? null);
  /** Medoid model id of the representative (first) well-defined region (caption). */
  ensembleMedoidModel = computed(() => this.ensembleRegions()[0]?.medoid_model_id ?? null);
  /** Author-provided representative model id and its selection criterion (caption,
   * shown only when a selection criterion was provided). */
  ensembleRepresentativeModel = computed(() => this.ensemble()?.representative_model_id ?? null);
  ensembleSelectionCriteria = computed(() => this.ensemble()?.selection_criteria ?? null);

  /** Cluster-analysis rows (cluster_id === -1 marks the single-model clusters). */
  ensembleClusters = computed<StatEnsembleCluster[]>(() => this.ensemble()?.cluster_analysis ?? []);
  hasClusters = computed(() => this.ensembleClusters().length > 0);
  /** Number of genuine (multi-model) clusters, i.e. excluding cluster_id === -1. */
  clusteredCount = computed(
    () => this.ensembleClusters().filter((c) => c.cluster_id !== -1).length,
  );
  /** Count of single-model clusters = size of the cluster_id === -1 model list. */
  singleModelCount = computed(
    () => this.ensembleClusters().find((c) => c.cluster_id === -1)?.model_ids?.length ?? 0,
  );

  /** PCA scatter (PC1 vs PC2) — one series per cluster (cluster_id === -1 →
   * 'Single-model'); each point carries its cluster and model in the tooltip.
   * Null when no cluster reports principal components. */
  ensemblePcaChart = computed<{ option: object; marginX: number; marginY: number } | null>(() => {
    const series = this.ensembleClusters()
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
    const legendW = this.legendReserve(names);
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
  });

  /** Entry information card (Property/Value): the output-file/entry fields. The
   * model file is a separate group (modelProps), rendered below a divider. */
  entryProps = computed<KVRow[]>(() => {
    const s = this.statistics();
    if (!s) return [];
    return [
      this.kv('File name', s.file_name),
      this.kv('File type', s.file_type && this.typeLabel(s.file_type)),
      this.kv('Entry ID', s.entry_id),
      this.kv('Entry title', s.entry_title?.trim()),
      this.kv('Entry authors', s.entry_authors ?? undefined),
      this.kv('Submission date', s.submission_date ?? undefined),
      this.kv('Processed date', s.processed_date ?? undefined),
      this.kv('Processed site', s.processed_site),
      this.kv('File size', s.file_size != null ? this.formatSize(s.file_size) : undefined),
      this.kv('MD5 checksum', s.md5_checksum),
    ].filter((r): r is KVRow => r !== null);
  });

  /** Model-file fields of the Entry card (shown after a divider when present). */
  modelProps = computed<KVRow[]>(() => {
    const m = this.statistics()?.model;
    if (!m) return [];
    return [
      this.kv('File name', m.file_name),
      this.kv('Entry title', m.struct_title?.trim()),
      this.kv('File type', m.file_type && this.typeLabel(m.file_type)),
      this.kv('Entry authors', m.audit_authors),
      this.kv('File size', m.file_size != null ? this.formatSize(m.file_size) : undefined),
      this.kv('MD5 checksum', m.md5_checksum),
    ].filter((r): r is KVRow => r !== null);
  });

  /** Assembly information card (Property/Value); the components list is a table. */
  assemblyProps = computed<KVRow[]>(() => {
    const a = this.statistics()?.assembly;
    if (!a) return [];
    return [
      this.kv('Name', a.name),
      this.kv('Number of components', a.number_of_components),
      this.kv('Organic ligands', a.organic_ligands),
      this.kv('Metal ions', a.metal_ions),
      this.kv('Non-standard bonds', this.yesNo(a.non_standard_bonds)),
      this.kv('Paramagnetic', this.yesNo(a.paramagnetic)),
      this.kv('Thiol state', a.thiol_state),
      this.kv('Molecular mass (Da)', a.molecular_mass),
    ].filter((r): r is KVRow => r !== null);
  });

  /** Per-component rows of the molecular assembly (table). */
  entityAssemblyRows = computed<StatEntityAssembly[]>(
    () => this.statistics()?.assembly?.entity_assembly ?? [],
  );
  /** Entity declarations (table). */
  entityRows = computed<StatEntity[]>(() => this.statistics()?.entity ?? []);

  /** One-letter polymer sequences, shown as <pre> blocks under the entity table
   * (only for entities that carry a sequence). */
  entitySequences = computed<{ entity_id?: number; seq: string }[]>(() =>
    this.entityRows()
      .filter((e) => !!e.polymer_seq_one_letter_code)
      .map((e) => ({ entity_id: e.entity_id, seq: e.polymer_seq_one_letter_code as string })),
  );
  /** Software used in the conversion (table). */
  softwareRows = computed<StatSoftware[]>(() => this.statistics()?.software ?? []);

  /** Chemical shift validation card (Property/Value): overall completeness of the
   * assigned chemical shifts, well-defined region. completeness_* fractions are
   * shown as percent. The full-length region is a separate group
   * (chemShiftFullProps), rendered below a divider. */
  chemShiftProps = computed<KVRow[]>(() => {
    const c = this.statistics()?.chem_shift_summary;
    if (!c) return [];
    return [
      this.kv(
        'Total number of shifts (well-defined region)',
        this.assignedOf(
          c.number_of_assigned_shifts_in_well_defined_region,
          c.number_of_target_shifts_in_well_defined_region,
        ),
      ),
      this.kv(
        'Number of shift outliers (well-defined region)',
        this.outliers(
          c.number_of_assigned_shifts_in_well_defined_region,
          c.number_of_favorable_assigned_shifts_in_well_defined_region,
        ),
      ),
      this.kv(
        'Completeness of assignment (well-defined region)',
        this.pct(c.completeness_in_well_defined_region),
      ),
    ].filter((r): r is KVRow => r !== null);
  });

  /** Full-length-region chem-shift completeness (shown after a divider). */
  chemShiftFullProps = computed<KVRow[]>(() => {
    const c = this.statistics()?.chem_shift_summary;
    if (!c) return [];
    return [
      this.kv(
        'Total number of shifts (full-length)',
        this.assignedOf(
          c.number_of_assigned_shifts_in_full_length_region,
          c.number_of_target_shifts_in_full_length_region,
        ),
      ),
      this.kv(
        'Number of shift outliers (full-length)',
        this.outliers(
          c.number_of_assigned_shifts_in_full_length_region,
          c.number_of_favorable_assigned_shifts_in_full_length_region,
        ),
      ),
      this.kv(
        'Completeness of assignment (full-length)',
        this.pct(c.completeness_in_full_length_region),
      ),
    ].filter((r): r is KVRow => r !== null);
  });

  /** Per-saveframe assigned-chemical-shift bookkeeping (output_statistics.chem_shift),
   * one Property/Value group per saveframe under the summary table. `unmapped` holds
   * the assigned shifts not mapped to the coordinate model (collapsible table). */
  chemShiftSaveframes = computed<
    {
      title: string;
      listId: string;
      rows: KVRow[];
      unmapped: StatChemShiftUnmapped[];
      unmappedCount: number;
      showInsCode: boolean;
      outlier: StatChemShiftOutlier[];
      outlierCount: number;
      showOutlierInsCode: boolean;
      showOutlierDetails: boolean;
      unparsed: StatChemShiftUnparsed[];
      unparsedCount: number;
      showUnparsedInsCode: boolean;
      duplicated: StatChemShiftUnmapped[];
      duplicatedCount: number;
      showDuplicatedInsCode: boolean;
      completeness: { phrase: string; view: CompletenessView }[];
      atomNameMapping: AtomNameMappingRow[];
      histograms: { title: string; option: object }[];
      rciPanels: { title: string; option: object }[];
    }[]
  >(() =>
    (this.statistics()?.chem_shift ?? []).map((s) => {
      const unmapped = s.chemical_shift_unmapped ?? [];
      const outlier = s.chemical_shift_outlier ?? [];
      const unparsed = s.chemical_shift_unparsed ?? [];
      const duplicated = s.chemical_shift_duplicated ?? [];
      const hasInsCode = (rows: { ins_code?: string | null }[]) =>
        rows.some((r) => r.ins_code != null && r.ins_code !== '');
      // Completeness pivot tables: well-defined regions and the full structure.
      const completeness = (
        [
          {
            phrase: 'well-defined regions of the structure',
            region: s.completeness_in_well_defined_region,
          },
          { phrase: 'full structure', region: s.completeness_in_full_length_region },
        ] as const
      ).flatMap(({ phrase, region }) => {
        const view = buildCompletenessView(region);
        return view ? [{ phrase, view }] : [];
      });
      return {
        title: `5.${s.list_id} Bookkeeping — ${s.sf_framecode} (${s.original_file_name})`.trim(),
        listId: `${s.list_id}`,
        rows: [
          this.kv('Number of parsed shifts', s.number_of_parsed),
          this.kv('Number of shifts mapped to model', s.number_of_mapped_to_model),
          this.kv('Number of shifts unmapped to model', s.number_of_unmapped_to_model),
          this.kv('Number of unparsed shifts with error', s.number_of_unparsed_with_error),
          this.kv('Number of parsed shifts with warning', s.number_of_parsed_with_warning),
          this.kv('Number of chemical shift outliers', s.number_of_outliers),
        ].filter((r): r is KVRow => r !== null),
        unmapped,
        unmappedCount: s.number_of_unmapped_to_model ?? unmapped.length,
        // Hide the Ins code column when no row carries an insertion code.
        showInsCode: hasInsCode(unmapped),
        outlier,
        outlierCount: s.number_of_outliers ?? outlier.length,
        showOutlierInsCode: hasInsCode(outlier),
        // Hide the Details column when no outlier carries a structural factor.
        showOutlierDetails: outlier.some((o) => o.details != null && o.details !== ''),
        unparsed,
        unparsedCount: s.number_of_unparsed_with_error ?? unparsed.length,
        showUnparsedInsCode: hasInsCode(unparsed),
        // No bookkeeping count for duplicates; use the row count itself.
        duplicated,
        duplicatedCount: duplicated.length,
        showDuplicatedInsCode: hasInsCode(duplicated),
        completeness,
        atomNameMapping: s.atom_name_mapping ?? [],
        // Normalized (Z-score) assigned-chemical-shift histogram(s).
        histograms: (s.histogram ?? []).map((h) => ({
          title: 'Normalized assigned chemical shifts (Z-score)',
          option: this.histogramOption(h, 'Z-score', '# of chemical shifts', {
            inverse: true,
            rangeLabels: true,
          }),
        })),
        // RCI/S² and NMR-RMSD per-residue plots (chain = Auth_asym_ID).
        rciPanels: (s.rci ?? []).map((c) => ({
          title: `${c.label} — Auth_asym_ID: ${c.chain}`,
          option: this.lineOption(c),
        })),
      };
    }),
  );

  /** ECharts option for a normalized chemical-shift histogram (ported from the
   * summary page). Bars are stacked per isotope; optional Z-score outlier markers
   * are drawn as dashed markLines against a hidden value axis. */
  private histogramOption(
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

  /** Structural-band fill color by type (secondary structure + ensemble domain). */
  private bandColor(type: string): string {
    if (type === 'helix') return 'rgba(204,47,0,0.12)';
    if (type === 'strand') return 'rgba(0,156,209,0.12)';
    if (type === 'turn') return 'rgba(200,204,0,0.18)';
    if (type === 'core') return 'rgba(224,255,255,0.6)'; // well-defined core: lightcyan
    if (type === 'unmodeled') return 'rgba(211,211,211,0.55)'; // unmodeled residues: lightgray
    return 'rgba(120,120,120,0.08)';
  }
  /** More saturated band color used as a thin edge line on the band's sides. */
  private bandEdgeColor(type: string): string {
    if (type === 'helix') return 'rgba(204,47,0,0.55)';
    if (type === 'strand') return 'rgba(0,156,209,0.55)';
    if (type === 'turn') return 'rgba(200,204,0,0.65)';
    if (type === 'core') return 'rgba(0,181,204,0.6)';
    if (type === 'unmodeled') return 'rgba(150,150,150,0.6)';
    return 'rgba(120,120,120,0.4)';
  }

  /** Structural bands as a markArea overlay anchored to a hidden value axis
   * (xAxisIndex 1) whose value v maps to category fraction v/n, so [start, end+1]
   * covers the full bins of the band's first/last residue. Ported from the
   * summary page. */
  private bandOverlay(
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
            color: this.bandColor(b.type),
            borderColor: this.bandEdgeColor(b.type),
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

  /** ECharts option for a per-residue line chart (RCI/S² or NMR RMSD) with
   * structural bands and an optional well-defined-region threshold line. Ported
   * from the summary page. */
  private lineOption(c: PerResidueLine): object {
    const interval = Math.max(0, Math.ceil(c.categories.length / 24) - 1);
    const { markerAxis, holderSeries } = this.bandOverlay(c.categories, c.bands);
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

  /** NMR restraint validation card (Property/Value): the scalar restraint counts.
   * average_* and *violation* items are excluded (per requirements; the backend
   * also drops them), and only scalar values are shown. Labels are humanized from
   * the report's field names since the key set varies by entry. */
  restraintProps = computed<KVRow[]>(() => {
    const rs = this.statistics()?.restraint_summary;
    if (!rs) return [];
    return Object.entries(rs)
      .filter(
        ([k, v]) =>
          !/average|violation/i.test(k) &&
          !k.endsWith('_dist_types') &&
          (typeof v === 'number' || typeof v === 'string'),
      )
      .sort(([a], [b]) => this.restraintRank(a) - this.restraintRank(b))
      .map(([k, v]) => {
        // For *_bond/metal_coordination restraints, fold the sibling *_dist_types
        // subclass list into the value as "{count} ({subclasses})".
        const bond = k.match(
          /^(hydrogen_bond|disulfide_bond|diselenide_bond|metal_coordination)_restraints$/,
        );
        const types = bond ? rs[`${bond[1]}_dist_types`] : undefined;
        const subclasses = typeof types === 'string' ? this.distTypeSubclasses(types) : '';
        return {
          label: RESTRAINT_LABEL_HTML[k] ?? this.humanize(k),
          value: subclasses ? `${v} (${subclasses})` : String(v),
        };
      });
  });

  /** Per-model distance-violation bins (small/medium/large) for the
   * 'Average number of distance violations per model' table. */
  distViolationsPerModel = computed<ViolationBin[]>(() => {
    const rs = this.statistics()?.restraint_summary as Record<string, unknown> | undefined;
    const v = rs?.['average_number_of_dist_violations_per_model'];
    return Array.isArray(v) ? (v as ViolationBin[]) : [];
  });
  /** Per-model dihedral-angle-violation bins for the corresponding table. */
  dihedViolationsPerModel = computed<ViolationBin[]>(() => {
    const rs = this.statistics()?.restraint_summary as Record<string, unknown> | undefined;
    const v = rs?.['average_number_of_dihed_violations_per_model'];
    return Array.isArray(v) ? (v as ViolationBin[]) : [];
  });
  /** Per-category distance-violation summary rows for the violation-analysis table. */
  distViolationSummary = computed<ViolationSummaryRow[]>(() => {
    const rs = this.statistics()?.restraint_summary as Record<string, unknown> | undefined;
    const v = rs?.['dist_violation_summary'];
    return Array.isArray(v) ? (v as ViolationSummaryRow[]) : [];
  });
  /** Per-category dihedral-angle-violation summary rows. */
  dihedViolationSummary = computed<ViolationSummaryRow[]>(() => {
    const rs = this.statistics()?.restraint_summary as Record<string, unknown> | undefined;
    const v = rs?.['dihed_violation_summary'];
    return Array.isArray(v) ? (v as ViolationSummaryRow[]) : [];
  });

  /** Raw per-model violation rows (gate the 7.2 / 8.2 sections). */
  distViolationForEachModel = computed<Record<string, number | null>[]>(() => {
    const rs = this.statistics()?.restraint_summary as Record<string, unknown> | undefined;
    const v = rs?.['dist_violation_for_each_model'];
    return Array.isArray(v) ? (v as Record<string, number | null>[]) : [];
  });
  dihedViolationForEachModel = computed<Record<string, number | null>[]>(() => {
    const rs = this.statistics()?.restraint_summary as Record<string, unknown> | undefined;
    const v = rs?.['dihed_violation_for_each_model'];
    return Array.isArray(v) ? (v as Record<string, number | null>[]) : [];
  });

  /** Per-model violation statistics table (fixed distance columns). */
  distModelViolations = computed<{
    columns: { key: string; label: string }[];
    rows: Record<string, number | null>[];
  } | null>(() => {
    const rs = this.statistics()?.restraint_summary as Record<string, unknown> | undefined;
    const rows = rs?.['dist_violation_for_each_model'];
    if (!Array.isArray(rows) || !rows.length) return null;
    const columns = [
      { key: 'ir_viol_count', label: 'IR' },
      { key: 'sq_viol_count', label: 'SQ' },
      { key: 'mr_viol_count', label: 'MR' },
      { key: 'lr_viol_count', label: 'LR' },
      { key: 'ic_viol_count', label: 'IC' },
      { key: 'total_viol_count', label: 'Total' },
    ];
    return { columns, rows: rows as Record<string, number | null>[] };
  });

  /** Per-model violation statistics table (dynamic dihedral-angle columns:
   * Phi, Psi, then any other angle types, Total last). */
  dihedModelViolations = computed<{
    columns: { key: string; label: string }[];
    rows: Record<string, number | null>[];
  } | null>(() => {
    const rs = this.statistics()?.restraint_summary as Record<string, unknown> | undefined;
    const raw = rs?.['dihed_violation_for_each_model'];
    if (!Array.isArray(raw) || !raw.length) return null;
    const rows = raw as Record<string, number | null>[];
    const seen = new Set<string>();
    for (const r of rows) {
      for (const k of Object.keys(r)) if (k.endsWith('_viol_count')) seen.add(k);
    }
    const fixed = ['phi_viol_count', 'psi_viol_count', 'total_viol_count'];
    const others = [...seen].filter((k) => !fixed.includes(k)).sort();
    const ordered = [
      ...(seen.has('phi_viol_count') ? ['phi_viol_count'] : []),
      ...(seen.has('psi_viol_count') ? ['psi_viol_count'] : []),
      ...others,
      ...(seen.has('total_viol_count') ? ['total_viol_count'] : []),
    ];
    const columns = ordered.map((k) => {
      const t = k.slice(0, -'_viol_count'.length);
      return { key: k, label: t.charAt(0).toUpperCase() + t.slice(1) };
    });
    return { columns, rows };
  });

  /** Raw per-ensemble violation-fraction rows (gate the 7.3 / 8.3 sections). */
  distViolationForEnsemble = computed<Record<string, number | null>[]>(() => {
    const rs = this.statistics()?.restraint_summary as Record<string, unknown> | undefined;
    const v = rs?.['dist_violation_for_ensemble'];
    return Array.isArray(v) ? (v as Record<string, number | null>[]) : [];
  });
  dihedViolationForEnsemble = computed<Record<string, number | null>[]>(() => {
    const rs = this.statistics()?.restraint_summary as Record<string, unknown> | undefined;
    const v = rs?.['dihed_violation_for_ensemble'];
    return Array.isArray(v) ? (v as Record<string, number | null>[]) : [];
  });

  /** Number of non-violated distance restraints (restraint − violated) overall and
   * per category, from the dist_violation_summary category rows (7.3 caption). */
  distNonViolated = computed(() => {
    const rows = this.distViolationSummary();
    const nv = (r?: ViolationSummaryRow) =>
      r ? (r.restraint_count ?? 0) - (r.viol_count ?? 0) : 0;
    const pre = (p: string) =>
      nv(rows.find((r) => (r.restraint_type ?? '').toLowerCase().startsWith(p)));
    return {
      total: nv(rows.find((r) => (r.restraint_type ?? '').toLowerCase() === 'total')),
      ir: pre('intra-residue'),
      sq: pre('sequential'),
      mr: pre('medium'),
      lr: pre('long'),
      ic: pre('inter-chain'),
    };
  });

  /** Non-violated dihedral-angle restraints (restraint − violated): the overall
   * total plus a per-angle-type breakdown string (angle names are dynamic). */
  dihedNonViolated = computed(() => {
    const rows = this.dihedViolationSummary();
    const nv = (r: ViolationSummaryRow) => (r.restraint_count ?? 0) - (r.viol_count ?? 0);
    const totalRow = rows.find((r) => (r.restraint_type ?? '').toLowerCase() === 'total');
    const perType = rows
      .filter((r) => (r.restraint_type ?? '').toLowerCase() !== 'total')
      .map((r) => {
        const t = r.restraint_type ?? '';
        return { label: t.charAt(0).toUpperCase() + t.slice(1), count: nv(r) };
      });
    return { total: totalRow ? nv(totalRow) : 0, perType };
  });

  /** Per-ensemble distance-violation table (fixed sub-type columns). */
  distEnsembleViolations = computed<{
    columns: { key: string; label: string }[];
    rows: Record<string, number | null>[];
  } | null>(() => {
    const rows = this.distViolationForEnsemble();
    if (!rows.length) return null;
    const columns = [
      { key: 'ir_viol_count', label: 'IR' },
      { key: 'sq_viol_count', label: 'SQ' },
      { key: 'mr_viol_count', label: 'MR' },
      { key: 'lr_viol_count', label: 'LR' },
      { key: 'ic_viol_count', label: 'IC' },
      { key: 'total_viol_count', label: 'Total' },
    ];
    return { columns, rows };
  });

  /** Per-ensemble dihedral-violation table (dynamic angle-type columns). */
  dihedEnsembleViolations = computed<{
    columns: { key: string; label: string }[];
    rows: Record<string, number | null>[];
  } | null>(() => {
    const rows = this.dihedViolationForEnsemble();
    if (!rows.length) return null;
    const seen = new Set<string>();
    for (const r of rows) {
      for (const k of Object.keys(r)) if (k.endsWith('_viol_count')) seen.add(k);
    }
    const fixed = ['phi_viol_count', 'psi_viol_count', 'total_viol_count'];
    const others = [...seen].filter((k) => !fixed.includes(k)).sort();
    const ordered = [
      ...(seen.has('phi_viol_count') ? ['phi_viol_count'] : []),
      ...(seen.has('psi_viol_count') ? ['psi_viol_count'] : []),
      ...others,
      ...(seen.has('total_viol_count') ? ['total_viol_count'] : []),
    ];
    const columns = ordered.map((k) => {
      const t = k.slice(0, -'_viol_count'.length);
      return { key: k, label: t.charAt(0).toUpperCase() + t.slice(1) };
    });
    return { columns, rows };
  });

  /** Stacked bar chart of violated restraints vs ensemble fraction (%), stacked
   * by category. cats without an explicit color use the default palette. */
  private violationEnsembleStackChart(
    rows: Record<string, number | null>[],
    cats: { key: string; label: string; color?: string }[],
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

  /** Per-ensemble distance-violation stacked bar chart (fixed sub-type categories). */
  distViolationsForEnsembleChart = computed<object | null>(() =>
    this.violationEnsembleStackChart(this.distViolationForEnsemble(), [
      { key: 'ir_viol_count', label: 'Intra-residue', color: '#5470c6' },
      { key: 'sq_viol_count', label: 'Sequential', color: '#a3c4f3' },
      { key: 'mr_viol_count', label: 'Medium range', color: '#3ba272' },
      { key: 'lr_viol_count', label: 'Long range', color: '#c0ca33' },
      { key: 'ic_viol_count', label: 'Inter-chain', color: '#808000' },
    ]),
  );

  /** Per-ensemble dihedral-violation stacked bar chart (dynamic angle types;
   * the aggregate 'total' column is excluded). */
  dihedViolationsForEnsembleChart = computed<object | null>(() => {
    const rows = this.dihedViolationForEnsemble();
    if (!rows.length) return null;
    const seen = new Set<string>();
    for (const r of rows) {
      for (const k of Object.keys(r)) if (k.endsWith('_viol_count')) seen.add(k);
    }
    const fixed = ['phi_viol_count', 'psi_viol_count', 'total_viol_count'];
    const others = [...seen].filter((k) => !fixed.includes(k)).sort();
    const ordered = [
      ...(seen.has('phi_viol_count') ? ['phi_viol_count'] : []),
      ...(seen.has('psi_viol_count') ? ['psi_viol_count'] : []),
      ...others,
    ];
    const cats = ordered.map((k) => {
      const t = k.slice(0, -'_viol_count'.length);
      return { key: k, label: t.charAt(0).toUpperCase() + t.slice(1) };
    });
    return this.violationEnsembleStackChart(rows, cats);
  });

  /** Most-violated restraint rows (restraint_summary.most_violated_*_restraints). */
  mostViolaratedDist = computed<MostViolatedRow[]>(() => {
    const rs = this.statistics()?.restraint_summary as Record<string, unknown> | undefined;
    const v = rs?.['most_violated_dist_restraints'];
    return Array.isArray(v) ? (v as MostViolatedRow[]) : [];
  });
  mostViolaratedDihed = computed<MostViolatedRow[]>(() => {
    const rs = this.statistics()?.restraint_summary as Record<string, unknown> | undefined;
    const v = rs?.['most_violated_dihed_restraints'];
    return Array.isArray(v) ? (v as MostViolatedRow[]) : [];
  });

  /** Hatch decal (diagonal lines) for the "Violated" overlay bars. */
  private static readonly VIOL_DECAL = {
    color: 'rgba(0, 0, 0, 0.55)',
    dashArrayX: [1, 0],
    dashArrayY: [2, 4],
    rotation: -Math.PI / 4,
  };

  /** Grouped bar chart of distance restraints (by sub-type) with the violated
   * (hatched) and consistently-violated (solid black) counts overlaid on each
   * restraint bar. Null when there are no distance restraints. */
  distViolationChart = computed<object | null>(() => {
    const rows = this.distViolationSummary();
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
    const byType = new Map(
      rows.map((r) => [(r.restraint_type ?? '').replace(/\s+/g, ''), r] as const),
    );
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
          itemStyle: { color: 'rgba(0,0,0,0.06)', decal: Download.VIOL_DECAL },
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
  });

  /** Bar chart of dihedral-angle restraints per angle type with the violated
   * (hatched) and consistently-violated (solid black) counts overlaid. */
  dihedViolationChart = computed<object | null>(() => {
    // Plot each individual dihedral-angle type (phi/psi/chi/alpha/…); the
    // aggregate "total" row is a summary, not a category, so it is excluded.
    const rows = this.dihedViolationSummary().filter(
      (r) => (r.restraint_type ?? '').toLowerCase() !== 'total',
    );
    if (!rows.length) return null;
    const xLabels = rows.map((r) => {
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
          data: rows.map((r) => r.restraint_count ?? 0),
          barGap: '-100%',
          barCategoryGap: '45%',
        },
        {
          name: 'Violated',
          type: 'bar',
          data: rows.map((r) => r.viol_count ?? 0),
          barGap: '-100%',
          itemStyle: { color: 'rgba(0,0,0,0.06)', decal: Download.VIOL_DECAL },
        },
        {
          name: 'Consistently violated',
          type: 'bar',
          data: rows.map((r) => r.consist_viol_count ?? 0),
          barGap: '-100%',
          itemStyle: { color: '#000' },
        },
      ],
    };
  });

  /** Dark-blue accent used for the per-model mean/median markers and error bars. */
  private static readonly MARK_COLOR = '#2b6cb0';
  /** Plus glyph in a 10×10 box; drawn with symbolRotate:45 to render as an "×"
   * (shared by the chart symbol and the tooltip marker so they match). */
  private static readonly MEDIAN_PATH =
    'M3,0 L7,0 L7,3 L10,3 L10,7 L7,7 L7,10 L3,10 L3,7 L0,7 L0,3 L3,3 Z';
  private static readonly MEDIAN_SYMBOL = `path://${Download.MEDIAN_PATH}`;

  /** renderItem for the mean±SD error bars (a custom series): a vertical I-beam
   * on the right (violation-magnitude) axis. */
  private errorBarRenderItem = (
    _params: unknown,
    api: { value(i: number): number; coord(p: number[]): number[] },
  ): object => {
    const low = api.coord([api.value(0), api.value(1)]);
    const high = api.coord([api.value(0), api.value(2)]);
    const x = low[0];
    const w = 4;
    const style = { stroke: Download.MARK_COLOR, lineWidth: 1 };
    return {
      type: 'group',
      children: [
        { type: 'line', shape: { x1: x, y1: low[1], x2: x, y2: high[1] }, style },
        { type: 'line', shape: { x1: x - w, y1: low[1], x2: x + w, y2: low[1] }, style },
        { type: 'line', shape: { x1: x - w, y1: high[1], x2: x + w, y2: high[1] }, style },
      ],
    };
  };

  /** Dual-axis per-model chart: stacked violation counts by category (left axis)
   * with mean (circle) / median (×) markers and mean±SD error bars on the right
   * (violation magnitude) axis. Null when there are no per-model rows. */
  private modelViolationChartOption(
    rows: Record<string, number | null>[],
    unit: string,
    cats: { key: string; label: string; color: string }[],
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
                  ? `<svg width="10" height="10" style="display:inline-block;vertical-align:middle;margin-right:5px"><path d="${Download.MEDIAN_PATH}" fill="${Download.MARK_COLOR}" transform="rotate(45 5 5)"/></svg>`
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
        { type: 'value', name: 'Number of violations', nameLocation: 'middle', nameGap: 44 },
        {
          type: 'value',
          name: `Mean, median violations (${unit})`,
          nameLocation: 'middle',
          nameGap: 44,
          position: 'right',
          min: 0,
          ...(rightAxisMax !== null ? { max: rightAxisMax } : {}),
          axisLine: { show: true, lineStyle: { color: Download.MARK_COLOR } },
          axisLabel: { color: Download.MARK_COLOR },
        },
      ],
      series: [
        ...cats.map((c) => ({
          name: c.label,
          type: 'bar',
          stack: 'v',
          yAxisIndex: 0,
          itemStyle: { color: c.color },
          data: rows.map((r) => num(r[c.key]) ?? 0),
        })),
        {
          name: 'Mean',
          type: 'scatter',
          yAxisIndex: 1,
          symbol: 'circle',
          symbolSize: 8,
          itemStyle: { color: Download.MARK_COLOR },
          data: scatter('mean_violation'),
        },
        {
          name: 'Median',
          type: 'scatter',
          yAxisIndex: 1,
          symbol: Download.MEDIAN_SYMBOL,
          symbolRotate: 45,
          symbolSize: 9,
          itemStyle: { color: Download.MARK_COLOR },
          data: scatter('median_violation'),
        },
        {
          name: 'Mean ± SD',
          type: 'custom',
          yAxisIndex: 1,
          silent: true,
          z: 5,
          renderItem: this.errorBarRenderItem,
          data: errorData,
        },
      ],
    };
  }

  /** Per-model distance-violation chart (fixed sub-type categories). */
  distModelViolationsChart = computed<object | null>(() =>
    this.modelViolationChartOption(this.distViolationForEachModel(), 'Å', [
      { key: 'ir_viol_count', label: 'Intra-residue', color: '#5470c6' },
      { key: 'sq_viol_count', label: 'Sequential', color: '#a3c4f3' },
      { key: 'mr_viol_count', label: 'Medium range', color: '#3ba272' },
      { key: 'lr_viol_count', label: 'Long range', color: '#c0ca33' },
      { key: 'ic_viol_count', label: 'Inter-chain', color: '#808000' },
    ]),
  );

  /** Per-model dihedral-angle-violation chart (PSI/PHI hard-coded). */
  dihedModelViolationsChart = computed<object | null>(() =>
    this.modelViolationChartOption(this.dihedViolationForEachModel(), '°', [
      { key: 'psi_viol_count', label: 'Psi', color: '#4a7ebb' },
      { key: 'phi_viol_count', label: 'Phi', color: '#9dc3e6' },
    ]),
  );

  /** Display label for a violation-summary restraint_type: underscores become
   * spaces; a leading abbreviation prefix ("ir;", "lr;", "total;", …) becomes a
   * two-space (non-breaking) indent and stays lower-case; top-level types have
   * their first character capitalized. */
  restraintTypeLabel(type: string | undefined): string {
    if (!type) return '';
    const semi = type.indexOf(';');
    if (semi >= 0) {
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

  /** Format a 0–100 percentage to one decimal place (empty string when null). */
  percent1(v: number | null | undefined): string {
    return v == null ? '' : v.toFixed(1);
  }

  /** Public conversion id (C_<id>) and the results zip file name. */
  publicId = computed(() => {
    const id = this.pageService.pageState().conversionId;
    return id === null ? '' : `C_${id}`;
  });
  zipName = computed(() => (this.publicId() ? `${this.publicId()}.zip` : ''));

  /** Tokenized URL that resumes/reviews the session (carries the token). */
  resumeUrl = computed(() => {
    const token = this.pageService.pageState().tokenBase;
    return token ? `${window.location.origin}/info?token=${token}` : '';
  });

  /** Direct link to the conversion-results zip (GET /api/download). */
  zipUrl = computed(() => {
    const token = this.pageService.pageState().tokenBase;
    return token ? `${API_URL}download?token=${encodeURIComponent(token)}` : '';
  });

  /** True once the results were downloaded — the session becomes read-only. */
  downloaded = computed(() => this.pageService.pageState().downloaded);

  /** All warnings acknowledged on the summary page (Terms #7) — gates download. */
  approved = computed(() => this.pageService.pageState().approved);

  /** Date (YYYY-MM-DD) until which the session and results stay accessible
   * (from GET /api/session via the page state). */
  expiryDate = computed(() => this.pageService.pageState().tokenExpiry);

  /** Optional recipient for the resume URL, and its verification state. */
  email = signal('');
  verifying = signal(false);
  /** Address passed format + MX verification → the Send button is enabled. */
  verified = signal(false);
  /** A verification attempt has completed for the current address. */
  emailChecked = signal(false);
  sending = signal(false);
  emailSent = signal(false);
  sendError = signal<string | null>(null);

  /** Editing the address invalidates any prior verification. */
  onEmailChange(value: string): void {
    this.email.set(value);
    this.verified.set(false);
    this.emailChecked.set(false);
  }

  /** Verify the address (format + MX deliverability) via POST /api/verify_email;
   * gates the Send button. Runs on blur. */
  verifyEmail(): void {
    const email = this.email().trim();
    if (!email || this.verifying()) return;
    this.verifying.set(true);
    this.http.post<{ valid: boolean }>(API_URL + 'verify_email', { email }).subscribe({
      next: (res) => {
        this.verified.set(!!res.valid);
        this.emailChecked.set(true);
        this.verifying.set(false);
      },
      error: (err) => {
        console.error('Failed to verify email', err);
        this.verified.set(false);
        this.emailChecked.set(true);
        this.verifying.set(false);
      },
    });
  }

  /** Email the resume URL to the verified recipient (POST /api/send_resume_url,
   * which sends the mail and logs it as a communication). */
  sendResumeUrl(): void {
    const token = this.pageService.pageState().tokenBase;
    if (!this.verified() || this.sending() || !token) return;
    this.sending.set(true);
    this.sendError.set(null);
    this.http.post(API_URL + 'send_resume_url', { token, email: this.email().trim() }).subscribe({
      next: () => {
        this.emailSent.set(true);
        this.sending.set(false);
      },
      error: (err) => {
        console.error('Failed to send resume URL', err);
        this.sendError.set('Could not send the email. Please try again later.');
        this.sending.set(false);
      },
    });
  }

  /** Download the results zip, then flip the session to read-only. GET
   * /api/download also persists downloaded=true and stamps the output files. */
  download(): void {
    const url = this.zipUrl();
    if (!url) return;
    window.location.href = url;
    this.pageService.pageState.update((prev) => ({ ...prev, downloaded: true }));
  }

  /** Human-readable label for an output_file_type. */
  typeLabel(fileType: string): string {
    return OUTPUT_TYPE_LABELS[fileType] ?? fileType;
  }

  /** Human-readable label for an upload (input) file type. */
  inputTypeLabel(value: string): string {
    return fileTypeLabel(value);
  }

  sourceLabel(source: string): string {
    return source === 'bmrb' ? 'BMRB' : 'User';
  }

  /** Date portion only of the naive-UTC upload timestamp ("YYYY-MM-DD"). */
  uploadDate(value: string | null): string {
    return value ? value.slice(0, 10) : '';
  }

  /** Build a Property/Value row, or null when the value is empty (so the caller
   * filters it out — omitted fields don't appear in the key-value cards). */
  private kv(label: string, value: string | number | undefined | null): KVRow | null {
    if (value === null || value === undefined || value === '') return null;
    return { label, value: String(value) };
  }

  /** Format an optional boolean as Yes/No (empty string when unset). */
  yesNo(value?: boolean): string {
    return value === null || value === undefined ? '' : value ? 'Yes' : 'No';
  }

  /** Format an optional 0–1 fraction as a percent string (undefined when unset). */
  private pct(value?: number): string | undefined {
    return value == null ? undefined : `${(value * 100).toFixed(1)}%`;
  }

  /** Shift outliers = assigned − favorable assigned (undefined unless both known). */
  private outliers(assigned?: number, favorable?: number): number | undefined {
    return assigned == null || favorable == null ? undefined : assigned - favorable;
  }

  /** Assigned-vs-target count as "{assigned} of {target}" (falls back to the
   * assigned count alone when the target is unknown; undefined when unassigned). */
  private assignedOf(assigned?: number, target?: number): string | undefined {
    if (assigned == null) return undefined;
    return target == null ? String(assigned) : `${assigned} of ${target}`;
  }

  /** Sort rank of a restraint_summary key (unlisted keys sort to the end). */
  private restraintRank(key: string): number {
    const i = RESTRAINT_KEY_ORDER.indexOf(key);
    return i === -1 ? RESTRAINT_KEY_ORDER.length : i;
  }

  /** Map a comma-separated *_dist_types code string to readable subclass names
   * (unknown codes are kept as-is). */
  private distTypeSubclasses(csv: string): string {
    return csv
      .split(',')
      .map((c) => c.trim())
      .filter((c) => c)
      .map((c) => DIST_TYPE_LABELS[c] ?? c)
      .join(', ');
  }

  /** Turn a report field name (snake/kebab case) into a readable label. */
  private humanize(key: string): string {
    const s = key.replace(/_/g, ' ');
    return s.charAt(0).toUpperCase() + s.slice(1);
  }

  /** Join an author-chain-id list for display in the entity table. */
  joinChains(value?: string[]): string {
    return (value ?? []).join(', ');
  }

  /** Format a byte count as a compact size string. */
  formatSize(bytes: number): string {
    if (!bytes) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(1024));
    return `${(bytes / Math.pow(1024, i)).toFixed(1)} ${units[i]}`;
  }

  /** Right-side legend width (px) sized to the longest series label, capped. */
  private static readonly LEGEND_CAP = 160;
  private legendReserve(names: string[]): number {
    const maxLen = names.reduce((m, n) => Math.max(m, n.length), 0);
    return Math.min(Download.LEGEND_CAP, Math.round(82 + 5.8 * maxLen));
  }
}
