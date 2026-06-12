import { Component, computed, effect, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom, TimeoutError } from 'rxjs';
import { timeout } from 'rxjs/operators';
import { ButtonModule } from 'primeng/button';
import { CardModule } from 'primeng/card';
import { CheckboxModule } from 'primeng/checkbox';
import { DialogModule } from 'primeng/dialog';
import { DividerModule } from 'primeng/divider';
import { InputNumberModule } from 'primeng/inputnumber';
import { MessageModule } from 'primeng/message';
import { RadioButtonModule } from 'primeng/radiobutton';
import { SelectModule } from 'primeng/select';

import { PageService, TargetDepsys } from './page.service';
import { API_URL } from '../../site.config';

type BmrbValidationState = 'idle' | 'validating' | 'valid';

interface BmrbEntryInfo {
  title: string;
  submitDate: string;
  releaseDate: string;
  authors: string;
}

interface FileRow {
  selected: boolean;
  name: string;
  size: number;
  fileType: string | null;
}

/** Outcome of the file requirement check for the selected deposition system. */
interface CheckResult {
  errors: string[];
  warnings: string[];
  recommendations: string[];
}

@Component({
  selector: 'app-upload',
  imports: [
    CommonModule,
    FormsModule,
    ButtonModule,
    CardModule,
    CheckboxModule,
    DialogModule,
    DividerModule,
    InputNumberModule,
    MessageModule,
    RadioButtonModule,
    SelectModule,
  ],
  templateUrl: './page.upload.html',
})
export class Upload {
  protected readonly TargetDepsys = TargetDepsys;

  private pageService = inject(PageService);
  private http = inject(HttpClient);
  readonly state = this.pageService.pageState;

  constructor() {
    // Restore BMRB import section when the component is created and a related
    // BMRB ID is already present in the session state (page refresh / resume).
    effect(() => {
      const relatedBmrbId = this.state().relatedBmrbId;
      if (relatedBmrbId !== null && this.bmrbId() === null) {
        this.importBmrbEntry.set(true);
        void this.onBmrbIdChange(relatedBmrbId);
      }
    });
  }

  /** Hidden once conversion ID is issued. */
  showSetupSection = computed(() => this.state().conversionId === null);

  /** Choose Files is disabled after the session is locked or downloaded. */
  isLocked = computed(
    () => this.state().lockedSession || this.state().expiredSession || !this.state().firstUpload,
  );

  importBmrbEntry = signal(false);
  bmrbId = signal<number | null>(null);
  bmrbValidationState = signal<BmrbValidationState>('idle');
  bmrbEntryInfo = signal<BmrbEntryInfo | null>(null);
  bmrbLinkedPdbId = signal<string | null>(null);
  /** Non-null when an error dialog should be shown; cleared on dialog close. */
  bmrbErrorMessage = signal<string | null>(null);

  /** True when a value is present but outside the 5-digit range (10000–99999). */
  isBmrbIdInvalidFormat = computed(() => {
    const id = this.bmrbId();
    return id !== null && (id < 10000 || id > 99999);
  });

  private validationVersion = 0;
  private readonly BMRB_API = 'https://api.bmrb.io/v2';

  rows = signal<FileRow[]>([]);

  /**
   * File extensions we refuse: tar archives and known compressed / binary
   * formats. Uploaded files must be plain text and uncompressed. Matched
   * case-insensitively against the trailing part of the file name.
   */
  private readonly FORBIDDEN_SUFFIXES = [
    '.tar',
    '.tar.gz',
    '.tgz',
    '.tar.bz2',
    '.tbz',
    '.tbz2',
    '.tar.xz',
    '.txz',
    '.tar.z',
    '.gz',
    '.gzip',
    '.bz2',
    '.xz',
    '.lz',
    '.lzma',
    '.zst',
    '.z',
    '.zip',
    '.7z',
    '.rar',
    '.cab',
    '.arj',
    '.ace',
  ];

  /**
   * Per-deposition-system file requirement check. Produces error, warning, and
   * recommendation messages displayed above the "Process selected files"
   * button. Errors and warnings block processing; recommendations are advisory.
   * NMR-data-specific conditions will be added here per target system.
   */
  checks = computed<CheckResult>(() => {
    const errors: string[] = [];
    const warnings: string[] = [];
    const recommendations: string[] = [];

    const rows = this.rows();
    const selected = rows.filter((r) => r.selected);

    // 1. No tar archives, compressed, or other binary files.
    const hasForbidden = rows.some((r) => {
      const lower = r.name.toLowerCase();
      return this.FORBIDDEN_SUFFIXES.some((s) => lower.endsWith(s));
    });
    if (hasForbidden) {
      errors.push('You cannot upload tar archive, compressed or other binary files.');
    }

    // 2. Every uploaded file must have a file type assigned.
    if (rows.some((r) => r.fileType === null)) {
      errors.push('Please define the types for all files.');
    }

    // 3. Coordinate file: OneDep and Replacing-CS require exactly one active
    //    (selected) coordinate file (co-cif or co-pdb).
    const target = this.state().targetDepsys;
    if (target === TargetDepsys.onedep || target === TargetDepsys.repl_cs) {
      const coordCount = selected.filter((r) => r.fileType?.startsWith('co-')).length;
      if (coordCount === 0) {
        errors.push('Please provide/select one coordinate file.');
      } else if (coordCount > 1) {
        errors.push('Please select one coordinate file only.');
      }
    }

    // 4. NMR unified data: at most one active (selected) nm-uni-* file is allowed.
    const uniCount = selected.filter((r) => r.fileType?.startsWith('nm-uni-')).length;
    if (uniCount > 1) {
      errors.push('You can upload a single NMR unified data file.');
    }

    // 5. Replacing-CS: requires exactly one NMR-STAR V3 unified data file
    //    (processed by OneDep); NEF is not supported.
    if (target === TargetDepsys.repl_cs) {
      const hasNef = selected.some((r) => r.fileType === 'nm-uni-nef');
      if (hasNef) {
        errors.push(
          'Only NMR-STAR format data file processed by OneDep can be uploaded, NEF is not supported.',
        );
      }
      const starCount = selected.filter((r) => r.fileType === 'nm-uni-str').length;
      if (starCount === 0) {
        errors.push('Please provide/select one NMR unified data file (NMR-STAR V3 format).');
      }
      // At least one assigned chemical shift file is mandatory (multiple allowed).
      const shiftCount = selected.filter((r) => r.fileType?.startsWith('nm-shi')).length;
      if (shiftCount === 0) {
        warnings.push('Please upload correct assigned chemical shifts in NMR-STAR V3 format.');
      }
      // Coordinate file should be PDBx/mmCIF, not legacy PDB.
      if (selected.some((r) => r.fileType === 'co-pdb')) {
        recommendations.push(
          'To preserve consistency between coordinates and NMR data, we strongly recommended uploading coordinate file in PDBx/mmCIF format processed with OneDep.',
        );
      }
    }

    // 6. BMRBdep: at least one assigned chemical shift file is mandatory; a
    //    topology file is optional but at most one is accepted.
    if (target === TargetDepsys.bmrbdep) {
      const shiftCount = selected.filter((r) => r.fileType?.startsWith('nm-shi')).length;
      if (shiftCount === 0) {
        warnings.push('Please upload at least one assigned chemical shift file.');
      }
      const topoCount = selected.filter((r) => r.fileType?.startsWith('nm-aux-')).length;
      if (topoCount > 1) {
        errors.push('Please select only one topology file.');
      }
    }

    // 7. OneDep combined deposition is exclusive: a selected NMR unified data
    //    file (alongside coordinates) cannot be mixed with separated chemical
    //    shift, restraint, peak list, or topology files.
    if (target === TargetDepsys.onedep && uniCount > 0) {
      const hasSeparated = selected.some(
        (r) =>
          r.fileType?.startsWith('nm-shi') ||
          r.fileType?.startsWith('nm-res-') ||
          r.fileType?.startsWith('nm-pea-') ||
          r.fileType?.startsWith('nm-aux-'),
      );
      if (hasSeparated) {
        errors.push(
          'Combined deposition allows only the coordinate file and a single NMR unified data file; separated chemical shift, restraint, peak list, or topology files are not allowed alongside it.',
        );
      }
    }

    // 8. OneDep conventional deposition (no NMR unified data file): requires
    //    assigned chemical shifts and at least one restraint file.
    if (target === TargetDepsys.onedep && uniCount === 0) {
      // Assigned chemical shifts are mandatory, unless a valid related BMRB ID
      // supplies them (downloaded from BMRB).
      const hasShifts = selected.some((r) => r.fileType?.startsWith('nm-shi'));
      const bmrbProvidesShifts = this.state().relatedBmrbId !== null;
      if (!hasShifts && !bmrbProvidesShifts) {
        errors.push(
          'Deposition of assigned chemical shifts is mandatory. Please upload them in NMR-STAR V3 format, or specify a valid related BMRB ID.',
        );
      }
      const hasRestraint = selected.some((r) => r.fileType?.startsWith('nm-res-'));
      if (!hasRestraint) {
        errors.push(
          'Deposition of NMR restraints is mandatory. Please upload each type of restraints in a separate file.',
        );
      }
      const hasPeak = selected.some((r) => r.fileType?.startsWith('nm-pea-'));
      if (!hasPeak) {
        recommendations.push('Deposition of spectral peak list is strongly encouraged.');
      }

      // Topology files are optional auxiliary data but strongly coupled with
      // their data type. When the coupled data file is present, exactly one
      // matching topology file is required; a topology file with no matching
      // data file is likewise rejected. AMBER/CHARMM/GROMACS pair a restraint
      // file with its topology; XEASY pairs its spectral peak list with the
      // .prot topology (nm-aux-xea).
      const TOPOLOGY_PAIRS = [
        { main: 'nm-res-amb', aux: 'nm-aux-amb', topo: 'AMBER topology file', mainKind: 'AMBER restraint' },
        { main: 'nm-res-cha', aux: 'nm-aux-cha', topo: 'CHARMM topology file', mainKind: 'CHARMM restraint' },
        { main: 'nm-res-gro', aux: 'nm-aux-gro', topo: 'GROMACS topology file', mainKind: 'GROMACS restraint' },
        {
          main: 'nm-pea-xea',
          aux: 'nm-aux-xea',
          topo: 'XEASY topology file (aka. prot)',
          mainKind: 'XEASY spectral peak list',
        },
      ];
      for (const { main, aux, topo, mainKind } of TOPOLOGY_PAIRS) {
        const mainCount = selected.filter((r) => r.fileType === main).length;
        const auxCount = selected.filter((r) => r.fileType === aux).length;
        if (mainCount > 0) {
          if (auxCount === 0) {
            errors.push(`Please upload one ${topo}.`);
          } else if (auxCount > 1) {
            errors.push(`Please select only one ${topo}.`);
          }
        } else if (auxCount > 0) {
          errors.push(`Please upload at least one ${mainKind} file.`);
        }
      }

      // Schrödinger/ASL is exceptional: its topology is a PDB-format file
      // (nm-aux-pdb). When the selected coordinate file is legacy PDB (co-pdb),
      // it already serves that purpose, so no separate topology is required.
      const hasSch = selected.some((r) => r.fileType === 'nm-res-sch');
      const coordIsLegacyPdb = selected.some((r) => r.fileType === 'co-pdb');
      if (hasSch && !coordIsLegacyPdb) {
        const pdbTopoCount = selected.filter((r) => r.fileType === 'nm-aux-pdb').length;
        if (pdbTopoCount === 0) {
          errors.push('Please upload one PDB topology file.');
        } else if (pdbTopoCount > 1) {
          errors.push('Please select only one PDB topology file.');
        }
      }
    }

    return { errors, warnings, recommendations };
  });

  /** Processing is allowed only when there are no errors and no warnings. */
  canProcess = computed(() => {
    const c = this.checks();
    return this.rows().length > 0 && c.errors.length === 0 && c.warnings.length === 0;
  });

  readonly depSystemOptions = [
    {
      label:
        'OneDep (<span class="italic underline">Conventional</span>: coordinates, assigned chemical shifts, NMR restraints) or (<span class="italic underline">Combined</span>: coordinates, NMR unified data)',
      value: TargetDepsys.onedep,
    },
    {
      label:
        'OneDep (<span class="italic underline">Replacing CS</span>: coordinates processed by OneDep, NMR unified data processed by OneDep, correct assigned chemical shifts)',
      value: TargetDepsys.repl_cs,
    },
    {
      label: 'BMRBdep (<span class="italic underline">BMRB Only</span>: assigned chemical shifts)',
      value: TargetDepsys.bmrbdep,
    },
  ];

  readonly FILE_TYPE_OPTIONS = [
    // Coordinate
    { label: 'Coordinates (PDBx/mmCIF format)', value: 'co-cif' },
    { label: 'Coordinates (PDB format)', value: 'co-pdb' },
    // NMR unified data
    { label: 'NMR unified data (NEF: NMR Exchange Format)', value: 'nm-uni-nef' },
    { label: 'NMR unified data (NMR-STAR V3 format)', value: 'nm-uni-str' },
    // Assigned chemical shifts
    { label: 'Assigned chemical shifts (NMR-STAR V3 format)', value: 'nm-shi' },
    { label: 'Assigned chemical shifts (ARIA format)', value: 'nm-shi-ari' },
    { label: 'Assigned chemical shifts (GARRET format)', value: 'nm-shi-gar' },
    { label: 'Assigned chemical shifts (NMRPIPE format)', value: 'nm-shi-npi' },
    { label: 'Assigned chemical shifts (OLIVIA format)', value: 'nm-shi-oli' },
    { label: 'Assigned chemical shifts (PIPP format)', value: 'nm-shi-pip' },
    { label: 'Assigned chemical shifts (NMRVIEW/CAMRA format)', value: 'nm-shi-ppm' },
    { label: 'Assigned chemical shifts (NMR-STAR V2 format, seq+cs loop)', value: 'nm-shi-st2' },
    { label: 'Assigned chemical shifts (XEASY format, aka. prot)', value: 'nm-shi-xea' },
    {
      label:
        "Assigned chemical shifts (WSV/TSV/CSV; Residue per line, Atom per line, or SPARKY's list)",
      value: 'nm-shi-bar',
    },
    // Spectral peak lists
    { label: 'Spectral peak list (ARIA format)', value: 'nm-pea-ari' },
    { label: 'Spectral peak list (CCPN format)', value: 'nm-pea-ccp' },
    { label: 'Spectral peak list (OLIVIA format)', value: 'nm-pea-oli' },
    { label: 'Spectral peak list (NMRPIPE/PIPP format)', value: 'nm-pea-pip' },
    { label: 'Spectral peak list (PONDEROSA format)', value: 'nm-pea-pon' },
    { label: 'Spectral peak list (SPARKY format)', value: 'nm-pea-spa' },
    { label: "Spectral peak list (SPARKY's save format, aka. ornament)", value: 'nm-pea-sps' },
    { label: 'Spectral peak list (TOPSPIN format)', value: 'nm-pea-top' },
    { label: 'Spectral peak list (NMRVIEW format)', value: 'nm-pea-vie' },
    { label: 'Spectral peak list (VNMR format)', value: 'nm-pea-vnm' },
    { label: 'Spectral peak list (XEASY format)', value: 'nm-pea-xea' },
    { label: 'Spectral peak list (XWINNMR format)', value: 'nm-pea-xwi' },
    { label: 'Spectral peak list (WSV/TSV with a header)', value: 'nm-pea-bar' },
    {
      label: 'Spectral peak list (any plane text format, auto format detection)',
      value: 'nm-pea-any',
    },
    // NMR restraints
    { label: 'NMR restraints (AMBER format)', value: 'nm-res-amb' },
    { label: 'NMR restraints (ARIA format)', value: 'nm-res-ari' },
    { label: 'NMR restraints (ARIA XML format)', value: 'nm-res-arx' },
    {
      label: "NMR restraints (WSV/TSV/CSV with a header; MARDIGAS, AQUA's noe, or User-defined)",
      value: 'nm-res-bar',
    },
    { label: 'NMR restraints (BIOSYM format, incl. INSIGHT-II)', value: 'nm-res-bio' },
    { label: 'NMR restraints (CHARMM format)', value: 'nm-res-cha' },
    { label: 'NMR restraints (CNS format)', value: 'nm-res-cns' },
    { label: 'NMR restraints (CYANA format)', value: 'nm-res-cya' },
    { label: 'NMR restraints (CYANA NOA format, aka. noe assignment)', value: 'nm-res-noa' },
    { label: 'NMR restraints (DYNAMO/PALES/TALOS format)', value: 'nm-res-dyn' },
    { label: 'NMR restraints (GROMACS format)', value: 'nm-res-gro' },
    { label: 'NMR restraints (ISD format)', value: 'nm-res-isd' },
    { label: 'NMR restraints (ROSETTA format)', value: 'nm-res-ros' },
    {
      label: 'NMR restraints (SAXS profile containing columns for q, I(q), σ(I))',
      value: 'nm-res-sax',
    },
    { label: 'NMR restraints (Schröginder/ASL format)', value: 'nm-res-sch' },
    { label: 'NMR restraints (SYBYL format)', value: 'nm-res-syb' },
    { label: 'NMR restraints (XPLOR-NIH format)', value: 'nm-res-xpl' },
    { label: 'NMR restraints (other plane text format)', value: 'nm-res-oth' },
    // Topology
    { label: 'Topology (AMBER format)', value: 'nm-aux-amb' },
    { label: 'Topology (CHARMM format)', value: 'nm-aux-cha' },
    { label: 'Topology (GROMACS format)', value: 'nm-aux-gro' },
    { label: 'Topology (PDB format)', value: 'nm-aux-pdb' },
    { label: 'Topology (XEASY format, aka. prot)', value: 'nm-aux-xea' },
  ];

  /**
   * Acceptable upload file types per target deposition system (exact value or
   * prefix match). Keep in sync with the file upload requirements cards.
   * - onedep : co-*, nm-uni-*, nm-shi, nm-pea-*, nm-res-*, nm-aux-*
   * - repl_cs: co-*, nm-uni-str, nm-shi
   * - bmrbdep: nm-uni-*, nm-shi, nm-shi-*, nm-aux-* (except nm-aux-xea)
   *
   * The XEASY .prot file carries both topology and chemical shifts, so
   * nm-aux-xea and nm-shi-xea are the same file. To avoid a redundant menu
   * entry, nm-aux-xea is offered only under OneDep (where nm-shi-xea is not
   * accepted and the XEASY peak list needs a topology); elsewhere the file is
   * uploaded as nm-shi-xea.
   */
  private readonly DEPSYS_FILE_TYPES: Record<TargetDepsys, (value: string) => boolean> = {
    [TargetDepsys.onedep]: (v) =>
      v.startsWith('co-') ||
      v.startsWith('nm-uni-') ||
      v === 'nm-shi' ||
      v.startsWith('nm-pea-') ||
      v.startsWith('nm-res-') ||
      v.startsWith('nm-aux-'),
    [TargetDepsys.repl_cs]: (v) => v.startsWith('co-') || v === 'nm-uni-str' || v === 'nm-shi',
    [TargetDepsys.bmrbdep]: (v) =>
      v.startsWith('nm-uni-') ||
      v === 'nm-shi' ||
      v.startsWith('nm-shi-') ||
      (v.startsWith('nm-aux-') && v !== 'nm-aux-xea'),
  };

  /** File type options suitable for the currently selected target deposition system. */
  fileTypeOptions = computed(() => {
    const allowed = this.DEPSYS_FILE_TYPES[this.state().targetDepsys];
    return this.FILE_TYPE_OPTIONS.filter((opt) => allowed(opt.value));
  });

  setTargetDepsys(value: TargetDepsys): void {
    this.pageService.pageState.update((prev) => ({ ...prev, targetDepsys: value }));
    // Clear any assigned file types no longer acceptable for the new target system.
    const allowed = this.DEPSYS_FILE_TYPES[value];
    this.rows.update((prev) =>
      prev.map((r) => (r.fileType && !allowed(r.fileType) ? { ...r, fileType: null } : r)),
    );
    if (value === TargetDepsys.bmrbdep) {
      this.importBmrbEntry.set(false);
      this.resetBmrbValidation();
    }
    this.persistDepsys(value, this.state().relatedBmrbId);
  }

  onImportBmrbChange(value: boolean): void {
    this.importBmrbEntry.set(value);
    if (!value) {
      this.resetBmrbValidation();
      this.persistDepsys(this.state().targetDepsys, null);
    }
  }

  onBmrbIdChange(value: number | null): void {
    this.bmrbId.set(value);
    this.bmrbEntryInfo.set(null);
    this.bmrbLinkedPdbId.set(null);

    if (value !== null && value >= 10000 && value <= 99999) {
      void this.validateBmrbId(value);
    } else if (value !== null && value > 99999) {
      // 6+ digits cannot be a valid BMRB ID
      this.bmrbErrorMessage.set('BMRB ID (> 5 digits) is invalid.');
      this.pageService.pageState.update((prev) => ({ ...prev, relatedBmrbId: null }));
    } else {
      this.bmrbValidationState.set('idle');
      this.pageService.pageState.update((prev) => ({ ...prev, relatedBmrbId: null }));
    }
  }

  onBmrbIdBlur(): void {
    const value = this.bmrbId();
    if (value !== null && value >= 1 && value <= 9999) {
      // Field lost focus with a 1–4 digit value — not a valid BMRB ID
      this.bmrbErrorMessage.set(
        'BMRB ID is outdated and unsuitable for associating with any PDB IDs.',
      );
      this.pageService.pageState.update((prev) => ({ ...prev, relatedBmrbId: null }));
    }
  }

  private resetBmrbValidation(): void {
    this.bmrbId.set(null);
    this.bmrbValidationState.set('idle');
    this.bmrbEntryInfo.set(null);
    this.bmrbLinkedPdbId.set(null);
    this.bmrbErrorMessage.set(null);
    this.pageService.pageState.update((prev) => ({ ...prev, relatedBmrbId: null }));
  }

  private async validateBmrbId(id: number): Promise<void> {
    const version = ++this.validationVersion;
    this.bmrbValidationState.set('validating');

    try {
      // Step 1: Confirm the entry exists in the macromolecule database
      const entryList = await firstValueFrom(
        this.http
          .get<unknown[]>(`${this.BMRB_API}/list_entries?database=macromolecules`)
          .pipe(timeout(10_000)),
      );
      if (version !== this.validationVersion) return;

      if (!entryList.some((e) => Number(e) === id)) {
        this.bmrbErrorMessage.set(
          'BMRB ID does not exist or is not publicly available yet. If you are the legitimated entry author, please ask BMRB annotator for information regarding the entry status.',
        );
        this.pageService.pageState.update((prev) => ({ ...prev, relatedBmrbId: null }));
        return;
      }

      // Step 2: Reject if the entry already has an exact PDB ID link
      const pdbLinks = await firstValueFrom(
        this.http
          .get<
            { pdb_id?: string; match_type: string }[]
          >(`${this.BMRB_API}/search/get_pdb_ids_from_bmrb_id/${id}`)
          .pipe(timeout(10_000)),
      );
      if (version !== this.validationVersion) return;

      const exact = pdbLinks.find((item) => item.match_type === 'Exact');
      if (exact) {
        this.bmrbLinkedPdbId.set(exact.pdb_id ?? 'unknown');
        this.bmrbErrorMessage.set(
          `BMRB ID has already been associated with PDB ID: ${exact.pdb_id ?? 'unknown'}. It is not possible to break the existing one-to-one entry relationship between BMRB and PDB`,
        );
        this.pageService.pageState.update((prev) => ({ ...prev, relatedBmrbId: null }));
        return;
      }

      // Step 3: Fetch title, submission date, release date, and author list in parallel
      const [titleResp, submitDateResp, releaseDateResp, authorsResp] = await Promise.all([
        firstValueFrom(
          this.http
            .get<Record<string, unknown>>(`${this.BMRB_API}/entry/${id}?tag=Entry.Title`)
            .pipe(timeout(10_000)),
        ),
        firstValueFrom(
          this.http
            .get<Record<string, unknown>>(`${this.BMRB_API}/entry/${id}?tag=Entry.Submission_date`)
            .pipe(timeout(10_000)),
        ),
        firstValueFrom(
          this.http
            .get<
              Record<string, unknown>
            >(`${this.BMRB_API}/entry/${id}?tag=Entry.Original_release_date`)
            .pipe(timeout(10_000)),
        ),
        firstValueFrom(
          this.http
            .get<Record<string, unknown>>(`${this.BMRB_API}/entry/${id}?loop=Entry_author`)
            .pipe(timeout(10_000)),
        ),
      ]);
      if (version !== this.validationVersion) return;

      this.bmrbEntryInfo.set({
        title: this.extractScalar(titleResp, id, 'Entry.Title'),
        submitDate: this.extractScalar(submitDateResp, id, 'Entry.Submission_date'),
        releaseDate: this.extractScalar(releaseDateResp, id, 'Entry.Original_release_date'),
        authors: this.extractAuthors(authorsResp, id),
      });
      this.bmrbValidationState.set('valid');
      this.pageService.pageState.update((prev) => ({
        ...prev,
        relatedBmrbId: id,
      }));
      this.persistDepsys(this.state().targetDepsys, id);
    } catch (err) {
      if (version !== this.validationVersion) return;
      this.bmrbErrorMessage.set(
        err instanceof TimeoutError
          ? 'BMRB API timeout error. Please try again later or contact us if you would like further investigation'
          : 'BMRB ID is invalid.',
      );
      this.pageService.pageState.update((prev) => ({
        ...prev,
        relatedBmrbId: null,
      }));
    }
  }

  /**
   * Extract a scalar value from a BMRB API tag response.
   * Actual format: { "<id>": { "Saveframe.Tag": ["value", ...] } }
   */
  private extractScalar(response: Record<string, unknown>, id: number, tagKey: string): string {
    const entry = (response[id] ?? response[String(id)]) as Record<string, unknown[]> | undefined;
    const values = entry?.[tagKey];
    return String(values?.[0] ?? '').trim();
  }

  /**
   * Build a formatted author list from a BMRB API loop response.
   * Actual format: { "<id>": { "Entry_author": [{ "tags": [...], "data": [[...], ...] }] } }
   * Column positions are mapped from "tags"; "." means null in NMR-STAR format.
   */
  private extractAuthors(response: Record<string, unknown>, id: number): string {
    const entry = (response[id] ?? response[String(id)]) as Record<string, unknown> | undefined;
    const loopArr = entry?.['Entry_author'] as { tags: string[]; data: string[][] }[] | undefined;
    if (!loopArr?.length) return '';

    const { tags = [], data = [] } = loopArr[0];
    const familyIdx = tags.indexOf('Family_name');
    const givenIdx = tags.indexOf('Given_name');
    if (familyIdx === -1) return '';

    return data
      .map((row) => {
        const familyRaw = row[familyIdx] ?? '';
        const givenRaw = givenIdx !== -1 ? (row[givenIdx] ?? '') : '';
        const family = familyRaw !== '.' ? familyRaw : '';
        const given = givenRaw !== '.' ? givenRaw : '';
        const initial = given ? given.charAt(0) : '';
        return initial ? `${family} ${initial}.` : family;
      })
      .filter(Boolean)
      .join(', ');
  }

  /** Close the BMRB error dialog, clear the input, and select "No". */
  onBmrbErrorClose(): void {
    this.bmrbErrorMessage.set(null);
    this.onImportBmrbChange(false);
  }

  private persistDepsys(depsys: TargetDepsys, relatedBmrbId: number | null): void {
    const token = this.state().tokenBase;
    if (!token) return;
    this.http
      .patch(API_URL + 'session', {
        token,
        target_depsys: TargetDepsys[depsys],
        related_bmrb_id: relatedBmrbId,
      })
      .subscribe({
        error: (err) => console.error('Failed to save deposition system', err),
      });
  }

  onFilesChosen(event: Event): void {
    const input = event.target as HTMLInputElement;
    if (!input.files?.length) return;
    const added: FileRow[] = Array.from(input.files).map((f) => ({
      selected: true,
      name: f.name,
      size: f.size,
      fileType: null,
    }));
    this.rows.update((prev) => [...prev, ...added]);
    input.value = '';
  }

  setSelected(index: number, value: boolean): void {
    this.rows.update((prev) => prev.map((r, i) => (i === index ? { ...r, selected: value } : r)));
  }

  setFileType(index: number, value: string): void {
    this.rows.update((prev) => prev.map((r, i) => (i === index ? { ...r, fileType: value } : r)));
  }

  removeRow(index: number): void {
    this.rows.update((prev) => prev.filter((_, i) => i !== index));
  }

  formatSize(bytes: number): string {
    if (bytes === 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(1024));
    return `${(bytes / Math.pow(1024, i)).toFixed(2)} ${units[i]}`;
  }

  processFiles(): void {
    // TODO: POST selected files to /api/upload with session token
    console.log(
      'Processing files:',
      this.rows().filter((r) => r.selected),
    );
  }
}
