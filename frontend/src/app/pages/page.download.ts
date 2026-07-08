import { Component, DestroyRef, computed, effect, inject, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { HttpClient } from '@angular/common/http';
import { RouterLink } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { CardModule } from 'primeng/card';
import { TableModule } from 'primeng/table';
import { ButtonModule } from 'primeng/button';
import { MessageModule } from 'primeng/message';
import { timer } from 'rxjs';
import { switchMap, takeWhile } from 'rxjs/operators';

import { PageService } from './page.service';
import { API_URL } from '../../site.config';

/** One conversion-result file bundled in the download zip. */
interface OutputFileRow {
  name: string;
  file_type: string;
  file_size: number;
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
  /** Overall NMR restraint counts. The backend already drops the average/violation
   * tables; values here are scalar counts keyed by (varied) report field names. */
  restraint_summary?: Record<string, unknown>;
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
  imports: [RouterLink, FormsModule, CardModule, TableModule, ButtonModule, MessageModule],
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
  }

  /** Fetch GET /api/output_statistics → the pruned output_statistics subtree. */
  private loadStatistics(token: string): void {
    this.http
      .get<{ available: boolean; statistics?: OutputStatistics }>(API_URL + 'output_statistics', {
        params: { token },
      })
      .subscribe({
        next: (res) => {
          this.statistics.set(res.statistics ?? null);
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

  /** The deferred NMR-STAR→NEF release is still running (poll until it clears). */
  nefGenerating = signal(false);

  /** A NEF file was produced — else the table shows the NEF-unavailable note. */
  hasNef = computed(() => this.files().some((f) => f.file_type === 'nef'));

  /** Conversion statistics subtree (GET /api/output_statistics); null until
   * loaded or when the report has no output_statistics. */
  statistics = signal<OutputStatistics | null>(null);
  /** Tri-state: null = loading, false = not available, true = show the cards. */
  statsAvailable = signal<boolean | null>(null);

  /** Entry information card (Property/Value): the output-file/entry fields. The
   * model file is a separate group (modelProps), rendered below a divider. */
  entryProps = computed<KVRow[]>(() => {
    const s = this.statistics();
    if (!s) return [];
    return [
      this.kv('Output file name', s.file_name),
      this.kv('Output file type', s.file_type && this.typeLabel(s.file_type)),
      this.kv('Entry ID', s.entry_id),
      this.kv('Entry title', s.entry_title?.trim()),
      this.kv('Entry authors', s.entry_authors ?? undefined),
      this.kv('Submission date', s.submission_date ?? undefined),
      this.kv('Processed date', s.processed_date ?? undefined),
      this.kv('Processed site', s.processed_site),
      this.kv('Output file size', s.file_size != null ? this.formatSize(s.file_size) : undefined),
      this.kv('MD5 checksum', s.md5_checksum),
    ].filter((r): r is KVRow => r !== null);
  });

  /** Model-file fields of the Entry card (shown after a divider when present). */
  modelProps = computed<KVRow[]>(() => {
    const m = this.statistics()?.model;
    if (!m) return [];
    return [
      this.kv('Model file name', m.file_name),
      this.kv('Model title', m.struct_title?.trim()),
      this.kv('Model file type', m.file_type && this.typeLabel(m.file_type)),
      this.kv('Model authors', m.audit_authors),
      this.kv('Model file size', m.file_size != null ? this.formatSize(m.file_size) : undefined),
      this.kv('Model MD5 checksum', m.md5_checksum),
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
        return { label: this.humanize(k), value: subclasses ? `${v} (${subclasses})` : String(v) };
      });
  });

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
