import { Component, DestroyRef, computed, effect, inject, signal, untracked } from '@angular/core';
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
import { AuthService } from './auth.service';
import { API_URL, HOST_SITE_URL } from '../../site.config';
import { fileTypeLabel } from './file-types';
import { EchartComponent } from './echart.component';
import {
  DIST_CAT_ORDER,
  dihedViolationChart as buildDihedViolationChart,
  distViolationChart as buildDistViolationChart,
  histogramOption,
  lineOption,
  meanViolationHistogram,
  modelViolationChartOption,
  pcaChartOption,
  rdcCorrelationChartOption,
  rdcViolationChart as buildRdcViolationChart,
  restraintTypeLabel as restraintTypeLabelImpl,
  stackedValueHistogram,
  violationEnsembleStackChart,
  legendReserve,
  type RdcCorrelationPlot,
} from './report-charts';

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
/** One per-model violation entry (restraint_summary.all_{dist,dihed}_violations). */
interface AllViolationRow {
  restraint_key?: string;
  distance_type?: string;
  dihedral_angle_name?: string;
  atom_key_1?: string;
  atom_key_2?: string;
  atom_key_3?: string;
  atom_key_4?: string;
  model_id?: number;
  violation?: number;
}

// ── RDC correlation preview (subset of GET /api/nmr_preview) ────────────────────
// The observed-vs-calculated RDC correlation scatter + Q-score tables live in the
// nmr_preview report (not output_statistics); the download page fetches just the
// RDC saveframes it needs to mirror the summary page's "9.1" content.
/** One RDC-restraint saveframe's observed-vs-calculated correlation scatter
 * (`name` = RDC vector type; point x/y are observed/calculated RDC in Hz). */
interface RdcCorrelationChart {
  label: string;
  correlation: RdcCorrelationPlot;
}
/** One RDC correlation quality-score row (per RDC vector type). */
interface RdcQScoreRow {
  type: string;
  count: number | null;
  r2: number | null;
  cornilescu_q: number | null;
  clore_q: number | null;
}
interface RdcQScoreTable {
  label: string;
  rows: RdcQScoreRow[];
}
/** The RDC-restraint saveframe fields the download page consumes. */
interface RdcRestraintPreviewSaveframe {
  sf_framecode: string;
  correlation: RdcCorrelationChart[];
  q_scores: RdcQScoreTable[];
}
interface NmrPreviewSubset {
  available: boolean;
  rdc_restraint_saveframes: RdcRestraintPreviewSaveframe[];
}
/** One ECharts panel: a title + the option, with optional square-aspect sizing. */
interface ChartPanel {
  title: string;
  option: object;
  aspect?: number;
  marginX?: number;
  marginY?: number;
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
  // Preserve the RDC acronym (the humanize fallback would lowercase it to "rdc").
  total_rdc_restraints: 'Total RDC restraints',
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
  private auth = inject(AuthService);
  private http = inject(HttpClient);
  private destroyRef = inject(DestroyRef);

  /** Canonical published JSON Schema the machine-readable conversion report
   * conforms to (matches the schema's own $id; served as a static asset). */
  readonly reportSchemaUrl = '/schema/nmr-data-processing-report-schema.json';

  constructor() {
    // Load the result-file listing once the session token is known, then keep
    // polling while either deferred step (NEF release, PDF report) is still
    // generating, so their rows appear on their own; stops once both clear.
    let started = false;
    effect(() => {
      const token = this.pageService.pageState().tokenBase;
      if (!token || started) return;
      started = true;
      timer(0, 5000)
        .pipe(
          switchMap(() =>
            this.http.get<{
              files: OutputFileRow[];
              nef_generating?: boolean;
              pdf_generating?: boolean;
            }>(API_URL + 'output_files', { params: { token } }),
          ),
          takeWhile((res) => !!res.nef_generating || !!res.pdf_generating, true),
          takeUntilDestroyed(this.destroyRef),
        )
        .subscribe({
          next: (res) => {
            this.files.set(res.files ?? []);
            this.nefGenerating.set(!!res.nef_generating);
            this.pdfGenerating.set(!!res.pdf_generating);
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

    // NMR preview (RDC correlation scatter + Q-scores) — a separate report from
    // output_statistics; fetched once for the "9.1" RDC correlation content.
    let previewFetched = false;
    effect(() => {
      const token = this.pageService.pageState().tokenBase;
      if (!token || previewFetched) return;
      previewFetched = true;
      this.loadNmrPreview(token);
    });

    // Load the upload (input) file listing once, for the provenance card.
    let inputFilesFetched = false;
    effect(() => {
      const token = this.pageService.pageState().tokenBase;
      if (!token || inputFilesFetched) return;
      inputFilesFetched = true;
      this.loadInputFiles(token);
    });

    // Refresh the (post-processing) session status once, so a failed run is
    // recognized here even when arriving from the summary via SPA navigation
    // (which skips a full session restore) — drives runFailed / the error notice.
    let statusRefreshed = false;
    effect(() => {
      const token = this.pageService.pageState().tokenBase;
      if (!token || statusRefreshed) return;
      statusRefreshed = true;
      this.pageService.refreshSession(token);
    });

    // `claimable`/`owned` are auth-dependent and fetched once at page load, so
    // re-check them whenever the user logs in (authenticated flips) or a claim
    // completes (claimRevision bumps) — otherwise the claim button only appears
    // after a manual refresh. untracked() reads the token without subscribing to
    // pageState, so refreshSession's own update can't re-trigger this effect.
    effect(() => {
      const authed = this.auth.authenticated();
      this.auth.claimRevision();
      untracked(() => {
        const token = this.pageService.pageState().tokenBase;
        if (authed && token) this.pageService.refreshSession(token);
      });
    });
  }

  // --- Adopt an anonymous session into the logged-in user's account --------- //

  /** Show the "save to my account" affordance: logged in, and this session is
   * unowned + non-expired (the backend sets `claimable`). Hidden once owned. */
  canClaim = computed(() => this.auth.authenticated() && this.pageService.pageState().claimable);
  /** Set once the user adopts the session via the button (transient confirmation;
   * auto-claim on login stays silent by design). */
  justClaimed = signal(false);
  claimBusy = signal(false);
  claimError = signal<string | null>(null);

  /** Explicit claim: bind this anonymous session to the account so it appears in
   * "My sessions". Auto-claim on login covers the common case; this is the manual
   * affordance. Idempotent server-side. */
  claimToMyAccount(): void {
    const token = this.pageService.pageState().tokenBase;
    if (!token || this.claimBusy()) return;
    this.claimBusy.set(true);
    this.claimError.set(null);
    this.auth.claimSession(token).subscribe({
      next: () => {
        this.claimBusy.set(false);
        this.justClaimed.set(true);
        this.pageService.markSessionClaimed();
      },
      error: () => {
        this.claimBusy.set(false);
        this.claimError.set('Could not save this conversion to your account — please retry.');
      },
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

  /** NMR preview subset (GET /api/nmr_preview) — the RDC correlation scatter and
   * Q-score tables, shared with the summary page's "9.1" content. */
  private nmrPreview = signal<NmrPreviewSubset | null>(null);
  /** RDC-restraint saveframes from the NMR preview (correlation + Q-scores). */
  rdcRestraintSaveframes = computed<RdcRestraintPreviewSaveframe[]>(
    () => this.nmrPreview()?.rdc_restraint_saveframes ?? [],
  );

  private loadNmrPreview(token: string): void {
    this.http.get<NmrPreviewSubset>(API_URL + 'nmr_preview', { params: { token } }).subscribe({
      next: (res) => this.nmrPreview.set(res),
      error: (err) => console.error('Failed to load NMR preview', err),
    });
  }

  /** Per-saveframe RDC correlation scatter panels (shared builder; square with a
   * y=x diagonal, marginX reserving the right-side legend). */
  rdcCorrelationPanels(sf: RdcRestraintPreviewSaveframe): ChartPanel[] {
    return sf.correlation.map((d) => ({
      title: 'Correlation between observed and calculated RDC values',
      option: rdcCorrelationChartOption(d.correlation),
      aspect: 1,
      marginX: 56 + legendReserve(d.correlation.groups.map((g) => g.name)),
      marginY: 56,
    }));
  }
  /** Per-saveframe RDC correlation quality-score tables (r²/Cornilescu-Q/Clore-Q
   * per RDC vector type). */
  rdcQScoreTables(sf: RdcRestraintPreviewSaveframe): RdcQScoreTable[] {
    return sf.q_scores;
  }

  /** Conversion result files bundled in the zip (GET /api/output_files). */
  files = signal<OutputFileRow[]>([]);

  /** Files the user uploaded for this conversion (GET /api/files). */
  inputFiles = signal<UploadFileRow[]>([]);
  /** Show the Source column only when a file did not come from the user. */
  showInputSource = computed(() => this.inputFiles().some((f) => f.source !== 'user'));

  /** The deferred NMR-STAR→NEF release is still running (poll until it clears). */
  nefGenerating = signal(false);

  /** The deferred JSON→PDF conversion report is still running; the download is
   * blocked until it clears so the Zip always carries the report. */
  pdfGenerating = signal(false);

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
  ensemblePcaChart = computed<{ option: object; marginX: number; marginY: number } | null>(() =>
    pcaChartOption(this.ensembleClusters()),
  );

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
          option: histogramOption(h, 'Z-score', 'Number of chemical shifts', {
            inverse: true,
            rangeLabels: true,
          }),
        })),
        // RCI/S² and NMR-RMSD per-residue plots (chain = Auth_asym_ID).
        rciPanels: (s.rci ?? []).map((c, index) => ({
          title: `${c.label} — Auth_asym_ID: ${c.chain}`,
          option: lineOption(c, index % 2 == 0 ? 'RCI / S² values' : 'NMR RMSD (Å)'),
        })),
      };
    }),
  );

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
  /** Per-model RDC-violation bins for the corresponding table. */
  rdcViolationsPerModel = computed<ViolationBin[]>(() => {
    const rs = this.statistics()?.restraint_summary as Record<string, unknown> | undefined;
    const v = rs?.['average_number_of_rdc_violations_per_model'];
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
  /** Per-category RDC-violation summary rows for the 9.1 violation-analysis table. */
  rdcViolationSummary = computed<ViolationSummaryRow[]>(() => {
    const rs = this.statistics()?.restraint_summary as Record<string, unknown> | undefined;
    const v = rs?.['rdc_violation_summary'];
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
  rdcViolationForEachModel = computed<Record<string, number | null>[]>(() => {
    const rs = this.statistics()?.restraint_summary as Record<string, unknown> | undefined;
    const v = rs?.['rdc_violation_for_each_model'];
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

  /** Ordered RDC per-model vector-type violation-count keys (excludes total). */
  private rdcModelTypeKeys(rows: Record<string, number | null>[]): string[] {
    const seen = new Set<string>();
    for (const r of rows) {
      for (const k of Object.keys(r))
        if (k.endsWith('_viol_count') && k !== 'total_viol_count') seen.add(k);
    }
    return [...seen].sort();
  }

  /** Per-model violation statistics table (dynamic RDC vector-type columns, plus
   * Total last). Labels use restraintTypeLabel to match the 9.1 summary table. */
  rdcModelViolations = computed<{
    columns: { key: string; label: string }[];
    rows: Record<string, number | null>[];
  } | null>(() => {
    const rows = this.rdcViolationForEachModel();
    if (!rows.length) return null;
    const keys = [...this.rdcModelTypeKeys(rows), 'total_viol_count'];
    const columns = keys.map((k) => ({
      key: k,
      label: this.restraintTypeLabel(k.slice(0, -'_viol_count'.length)),
    }));
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
  rdcViolationForEnsemble = computed<Record<string, number | null>[]>(() => {
    const rs = this.statistics()?.restraint_summary as Record<string, unknown> | undefined;
    const v = rs?.['rdc_violation_for_ensemble'];
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

  /** Non-violated RDC restraints (restraint − violated): the overall total plus
   * a per-vector-type breakdown (labels via restraintTypeLabel, matching 9.1). */
  rdcNonViolated = computed(() => {
    const rows = this.rdcViolationSummary();
    const nv = (r: ViolationSummaryRow) => (r.restraint_count ?? 0) - (r.viol_count ?? 0);
    const totalRow = rows.find((r) => (r.restraint_type ?? '').toLowerCase() === 'total');
    const perType = rows
      .filter((r) => (r.restraint_type ?? '').toLowerCase() !== 'total')
      .map((r) => ({ label: this.restraintTypeLabel(r.restraint_type), count: nv(r) }));
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

  /** Per-ensemble RDC-violation table (dynamic vector-type columns, Total last;
   * labels via restraintTypeLabel, matching the 9.1/9.2 tables). */
  rdcEnsembleViolations = computed<{
    columns: { key: string; label: string }[];
    rows: Record<string, number | null>[];
  } | null>(() => {
    const rows = this.rdcViolationForEnsemble();
    if (!rows.length) return null;
    const keys = [...this.rdcModelTypeKeys(rows), 'total_viol_count'];
    const columns = keys.map((k) => ({
      key: k,
      label: this.restraintTypeLabel(k.slice(0, -'_viol_count'.length)),
    }));
    return { columns, rows };
  });

  /** Per-ensemble distance-violation stacked bar chart (fixed sub-type categories). */
  distViolationsForEnsembleChart = computed<object | null>(() =>
    violationEnsembleStackChart(this.distViolationForEnsemble(), [
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
    return violationEnsembleStackChart(rows, cats);
  });

  /** Per-ensemble RDC-violation stacked bar chart (dynamic vector-type
   * categories from the data; the aggregate 'total' column is excluded). */
  rdcViolationsForEnsembleChart = computed<object | null>(() => {
    const rows = this.rdcViolationForEnsemble();
    if (!rows.length) return null;
    const cats = this.rdcModelTypeKeys(rows).map((k) => ({
      key: k,
      label: this.restraintTypeLabel(k.slice(0, -'_viol_count'.length)),
    }));
    return violationEnsembleStackChart(rows, cats);
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
  mostViolaratedRdc = computed<MostViolatedRow[]>(() => {
    const rs = this.statistics()?.restraint_summary as Record<string, unknown> | undefined;
    const v = rs?.['most_violated_rdc_restraints'];
    if (!Array.isArray(v)) return [];
    // RDC reuses the shared most-violated schema, whose only type slots are
    // distance_type / dihedral_angle_name; surface whichever the converter
    // populated (the RDC vector type) in distance_type so the table's Type column
    // and the mean-violation histogram categorise by it.
    return (v as MostViolatedRow[]).map((r) => ({
      ...r,
      distance_type: r.distance_type || r.dihedral_angle_name,
    }));
  });

  /** Distance mean-violation stacked histogram. */
  distMeanViolationHist = computed<object | null>(() =>
    meanViolationHistogram(this.mostViolaratedDist(), 'distance_type', 'Å', DIST_CAT_ORDER),
  );

  /** Dihedral-angle mean-violation stacked histogram (dynamic angle types). */
  dihedMeanViolationHist = computed<object | null>(() =>
    meanViolationHistogram(this.mostViolaratedDihed(), 'dihedral_angle_name', '°', ['phi', 'psi']),
  );

  /** RDC mean-violation stacked histogram (categorised by RDC vector type, which
   * mostViolaratedRdc normalises into distance_type). */
  rdcMeanViolationHist = computed<object | null>(() =>
    meanViolationHistogram(this.mostViolaratedRdc(), 'distance_type', 'Hz', []),
  );

  /** All per-model violation entries (restraint_summary.all_*_violations). */
  allViolaratedDist = computed<AllViolationRow[]>(() => {
    const rs = this.statistics()?.restraint_summary as Record<string, unknown> | undefined;
    const v = rs?.['all_dist_violations'];
    return Array.isArray(v) ? (v as AllViolationRow[]) : [];
  });
  allViolaratedDihed = computed<AllViolationRow[]>(() => {
    const rs = this.statistics()?.restraint_summary as Record<string, unknown> | undefined;
    const v = rs?.['all_dihed_violations'];
    return Array.isArray(v) ? (v as AllViolationRow[]) : [];
  });
  allViolaratedRdc = computed<AllViolationRow[]>(() => {
    const rs = this.statistics()?.restraint_summary as Record<string, unknown> | undefined;
    const v = rs?.['all_rdc_violations'];
    if (!Array.isArray(v)) return [];
    // RDC reuses the shared all-violation schema (type slots distance_type /
    // dihedral_angle_name only); surface whichever holds the vector type in
    // distance_type so the Type column and histogram categorise by it.
    return (v as AllViolationRow[]).map((r) => ({
      ...r,
      distance_type: r.distance_type || r.dihedral_angle_name,
    }));
  });

  /** Stacked histogram of every distance violation value, by restraint category. */
  distViolationHist = computed<object | null>(() => {
    const pts = this.allViolaratedDist()
      .map((r) => ({
        value: typeof r.violation === 'number' ? r.violation : null,
        cat: String(r.distance_type ?? ''),
      }))
      .filter((p): p is { value: number; cat: string } => p.value !== null && p.cat !== '');
    return stackedValueHistogram(pts, 'Å', DIST_CAT_ORDER, 'Violation');
  });
  /** Stacked histogram of every dihedral-angle violation value (dynamic types). */
  dihedViolationHist = computed<object | null>(() => {
    const pts = this.allViolaratedDihed()
      .map((r) => ({
        value: typeof r.violation === 'number' ? r.violation : null,
        cat: String(r.dihedral_angle_name ?? ''),
      }))
      .filter((p): p is { value: number; cat: string } => p.value !== null && p.cat !== '');
    return stackedValueHistogram(pts, '°', ['phi', 'psi'], 'Violation');
  });
  /** Stacked histogram of every RDC violation value, by vector type (normalised
   * into distance_type by allViolaratedRdc). */
  rdcViolationHist = computed<object | null>(() => {
    const pts = this.allViolaratedRdc()
      .map((r) => ({
        value: typeof r.violation === 'number' ? r.violation : null,
        cat: String(r.distance_type ?? ''),
      }))
      .filter((p): p is { value: number; cat: string } => p.value !== null && p.cat !== '');
    return stackedValueHistogram(pts, 'Hz', [], 'Violation');
  });

  /** Grouped bar chart of distance restraints (by sub-type) with the violated
   * (hatched) and consistently-violated (solid black) counts overlaid. */
  distViolationChart = computed<object | null>(() =>
    buildDistViolationChart(this.distViolationSummary()),
  );

  /** Bar chart of dihedral-angle restraints per angle type with the violated
   * (hatched) and consistently-violated (solid black) counts overlaid. */
  dihedViolationChart = computed<object | null>(() =>
    buildDihedViolationChart(this.dihedViolationSummary()),
  );

  /** Bar chart of RDC restraints per vector type with the violated (hatched) and
   * consistently-violated (solid black) counts overlaid. */
  rdcViolationChart = computed<object | null>(() =>
    buildRdcViolationChart(this.rdcViolationSummary()),
  );

  /** Per-model distance-violation chart (fixed sub-type categories). */
  distModelViolationsChart = computed<object | null>(() =>
    modelViolationChartOption(this.distViolationForEachModel(), 'Å', [
      { key: 'ir_viol_count', label: 'Intra-residue', color: '#5470c6' },
      { key: 'sq_viol_count', label: 'Sequential', color: '#a3c4f3' },
      { key: 'mr_viol_count', label: 'Medium range', color: '#3ba272' },
      { key: 'lr_viol_count', label: 'Long range', color: '#c0ca33' },
      { key: 'ic_viol_count', label: 'Inter-chain', color: '#808000' },
    ]),
  );

  /** Per-model dihedral-angle-violation chart (PSI/PHI hard-coded). */
  dihedModelViolationsChart = computed<object | null>(() =>
    // No explicit colors: use the default palette in the same order (Phi, Psi)
    // as the other dihedral-angle charts so the categories match across them.
    modelViolationChartOption(this.dihedViolationForEachModel(), '°', [
      { key: 'phi_viol_count', label: 'Phi' },
      { key: 'psi_viol_count', label: 'Psi' },
    ]),
  );

  /** Per-model RDC-violation chart (dynamic vector-type categories from the
   * data; default palette, matching the 9.1/9.2 tables). */
  rdcModelViolationsChart = computed<object | null>(() => {
    const rows = this.rdcViolationForEachModel();
    return modelViolationChartOption(
      rows,
      'Hz',
      this.rdcModelTypeKeys(rows).map((k) => ({
        key: k,
        label: this.restraintTypeLabel(k.slice(0, -'_viol_count'.length)),
      })),
    );
  });

  /** Display label for a violation-summary restraint_type: underscores become
   * spaces; a leading abbreviation prefix ("ir;", "lr;", "total;", …) becomes a
   * two-space (non-breaking) indent and stays lower-case; top-level types have
   * their first character capitalized. */
  restraintTypeLabel(type: string | undefined): string {
    return restraintTypeLabelImpl(type);
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

  /** The run did not complete (failed/aborted) — results are incomplete and must
   * not be downloadable; the download page shows an error notice instead. Shared
   * with the summary page via PageService so the two never diverge. */
  runFailed = this.pageService.sessionFailed;

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
}
