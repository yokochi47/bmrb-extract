import { Component, computed, effect, inject, OnDestroy, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Router } from '@angular/router';
import { firstValueFrom, Subscription, timer, TimeoutError } from 'rxjs';
import { switchMap, timeout } from 'rxjs/operators';
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
import { FILE_TYPE_OPTIONS } from './file-types';

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
  /** Local blob, used to upload the file to the server at process time. */
  file: File;
  /** Server-assigned ordinal; set once the file has been uploaded so a
   * re-process does not upload it again. */
  ordinal?: number;
}

/** Outcome of the file requirement check for the selected deposition system. */
interface CheckResult {
  errors: string[];
  warnings: string[];
  recommendations: string[];
}

/** One row in the processing dialog (mirrors GET /api/progress task entries). */
interface ProgressTask {
  task: string;
  label: string;
  status: string | null;
  report_status?: string | null;
  report_summary?: string | null;
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
export class Upload implements OnDestroy {
  protected readonly TargetDepsys = TargetDepsys;

  private pageService = inject(PageService);
  private http = inject(HttpClient);
  private router = inject(Router);
  readonly state = this.pageService.pageState;

  /** Guards the one-shot previous-status fetch so it runs once per component. */
  private previousChecked = false;

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

    // Once a conversion ID is known (a run has been started in this session,
    // including after a page refresh / resume), fetch the latest run's status
    // so the previous-upload banner reflects it. Skipped while the dialog is
    // already live from a fresh submit.
    effect(() => {
      const conversionId = this.state().conversionId;
      if (conversionId !== null && !this.previousChecked && !this.processing()) {
        this.previousChecked = true;
        this.checkPreviousStatus();
      }
    });
  }

  ngOnDestroy(): void {
    this.pollSub?.unsubscribe();
    this.logSub?.unsubscribe();
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

  /** Controls the "which assigned chemical shifts are authoritative?" dialog. */
  bmrbShiftConflict = signal(false);

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
      warnings.push('Please define the types for all files.');
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
      const shiftCount = selected.filter(
        (r) => r.fileType?.startsWith('nm-uni-') || r.fileType?.startsWith('nm-shi'),
      ).length;
      if (shiftCount === 0) {
        warnings.push('Please upload at least one assigned chemical shift file.');
      }
      const topoCount = selected.filter((r) => r.fileType?.startsWith('nm-aux-')).length;
      if (topoCount > 1) {
        errors.push('Please select only one topology file.');
      }
    }

    // 7. OneDep combined single file deposition is exclusive: a selected NMR unified data
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
          'Combined single file deposition allows only the coordinate file and the NMR unified data file; separated chemical shift, restraint, peak list, or topology files are not allowed alongside it.',
        );
      }
    }

    // 8. OneDep conventional separated file deposition (no NMR unified data file): requires
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
        {
          main: 'nm-res-amb',
          aux: 'nm-aux-amb',
          topo: 'AMBER topology file',
          mainKind: 'AMBER restraint',
        },
        {
          main: 'nm-res-cha',
          aux: 'nm-aux-cha',
          topo: 'CHARMM topology file',
          mainKind: 'CHARMM restraint',
        },
        {
          main: 'nm-res-gro',
          aux: 'nm-aux-gro',
          topo: 'GROMACS topology file',
          mainKind: 'GROMACS restraint',
        },
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
        'OneDep (<span class="italic underline">Conventional seperated</span>: coordinates, assigned chemical shifts, NMR restraints) or (<span class="italic underline">Combined single</span>: coordinates, NMR unified data)',
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

  // Canonical file-type catalogue lives in ./file-types (shared with the
  // summary page). Aliased here so the template keeps using FILE_TYPE_OPTIONS.
  protected readonly FILE_TYPE_OPTIONS = FILE_TYPE_OPTIONS;

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

  /**
   * File-name hints used to pre-populate the file-type select on upload, so a
   * recognised extension picks a sensible default and the user only has to
   * confirm. Keys are lower-cased extensions. The XEASY '.prot' file is
   * resolved per target in guessFileType() (topology vs chemical shifts), and
   * every guess is gated through DEPSYS_FILE_TYPES so a type that the current
   * deposition system does not accept is dropped (row left unselected).
   */
  private readonly EXT_FILE_TYPE: Record<string, string> = {
    rst: 'nm-res-amb',
    rest: 'nm-res-amb',
    amber: 'nm-res-amb',
    prmtop: 'nm-aux-amb',
    crd: 'nm-aux-cha',
    upl: 'nm-res-cya',
    lol: 'nm-res-cya',
    aco: 'nm-res-cya',
    upv: 'nm-res-cya',
    lov: 'nm-res-cya',
    cco: 'nm-res-cya',
    noa: 'nm-res-noa',
    asl: 'nm-res-sch',
    xpk: 'nm-pea-vie',
    peaks: 'nm-pea-xea',
    nef: 'nm-uni-nef',
    pdb: 'co-pdb',
    cif: 'co-cif',
  };

  /** Whole file names (no extension) that map to a type — complete matches. */
  private readonly NAME_FILE_TYPE: Record<string, string> = {
    rst: 'nm-res-amb',
    prmtop: 'nm-aux-amb',
  };

  /**
   * Best-guess upload file type from a file name for the given target, or null
   * when there is no confident match (or the match is not acceptable for the
   * target). Extension / whole-name matching is case-insensitive.
   */
  private guessFileType(name: string, target: TargetDepsys): string | null {
    const lower = name.toLowerCase();
    const dot = lower.lastIndexOf('.');
    const ext = dot >= 0 ? lower.slice(dot + 1) : '';
    let value: string | undefined = this.EXT_FILE_TYPE[ext] ?? this.NAME_FILE_TYPE[lower];
    // XEASY .prot carries topology (OneDep) or chemical shifts (elsewhere).
    if (ext === 'prot') value = target === TargetDepsys.onedep ? 'nm-aux-xea' : 'nm-shi-xea';
    if (!value) return null;
    return this.DEPSYS_FILE_TYPES[target](value) ? value : null;
  }

  /**
   * Menu sub-groups for the file-type select, in display order. The value
   * prefixes are mutually exclusive; 'nm-shi' covers both 'nm-shi' and 'nm-shi-*'.
   */
  private readonly FILE_TYPE_GROUPS: { label: string; match: (value: string) => boolean }[] = [
    { label: 'Coordinates', match: (v) => v.startsWith('co-') },
    { label: 'NMR unified data', match: (v) => v.startsWith('nm-uni-') },
    { label: 'Assigned chemical shifts', match: (v) => v.startsWith('nm-shi') },
    { label: 'NMR restraints', match: (v) => v.startsWith('nm-res-') },
    { label: 'Topology', match: (v) => v.startsWith('nm-aux-') },
    { label: 'Spectral peak lists', match: (v) => v.startsWith('nm-pea-') },
  ];

  /** Drop the redundant category prefix, keeping the in-parentheses descriptor
   * (greedy to the last ')', so nested parens are preserved). */
  private shortLabel(label: string): string {
    const m = label.match(/^[^(]*\((.*)\)\s*$/);
    return m ? m[1] : label;
  }

  /**
   * File type options for the current target deposition system, grouped under
   * the category headers with shortened item labels; empty groups are omitted.
   */
  fileTypeOptions = computed(() => {
    const allowed = this.DEPSYS_FILE_TYPES[this.state().targetDepsys];
    return this.FILE_TYPE_GROUPS.map((g) => ({
      label: g.label,
      items: this.FILE_TYPE_OPTIONS.filter((opt) => g.match(opt.value) && allowed(opt.value)).map(
        (opt) => {
          const short = this.shortLabel(opt.label);
          // Item list shows the short label; the closed select shows the
          // '{group} - {short}' selected label (e.g. 'Coordinates - PDBx/mmCIF format').
          return { label: short, selectedLabel: `${g.label} - ${short}`, value: opt.value };
        },
      ),
    })).filter((g) => g.items.length > 0);
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
    const target = this.state().targetDepsys;
    const added: FileRow[] = Array.from(input.files).map((f) => ({
      selected: true,
      name: f.name,
      size: f.size,
      fileType: this.guessFileType(f.name, target),
      file: f,
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

  /**
   * OneDep conventional separated file deposition mode only: a valid related BMRB ID supplies assigned
   * chemical shifts, but the user has also selected their own chemical-shift
   * file(s). Only the user can say which is authoritative. Combined single file mode (a
   * selected nm-uni-* file) ignores the BMRB ID, so there is no conflict.
   */
  private hasShiftConflict(): boolean {
    if (this.state().targetDepsys !== TargetDepsys.onedep) return false;
    if (this.state().relatedBmrbId === null) return false;
    const selected = this.rows().filter((r) => r.selected);
    if (selected.some((r) => r.fileType?.startsWith('nm-uni-'))) return false;
    return selected.some((r) => r.fileType?.startsWith('nm-shi'));
  }

  processFiles(): void {
    if (this.hasShiftConflict()) {
      this.bmrbShiftConflict.set(true);
      return;
    }
    this.submitProcessing();
  }

  /** BMRB archive chosen: deselect the user's own chemical-shift files so the
   * BMRB-downloaded shifts are the sole source, then process. */
  useBmrbShifts(): void {
    this.rows.update((prev) =>
      prev.map((r) => (r.fileType?.startsWith('nm-shi') ? { ...r, selected: false } : r)),
    );
    this.bmrbShiftConflict.set(false);
    this.submitProcessing();
  }

  /** Own file chosen: keep the selection; the backend then skips the BMRB
   * download because a user chemical-shift file remains selected. */
  useOwnShifts(): void {
    this.bmrbShiftConflict.set(false);
    this.submitProcessing();
  }

  /** True while files are being uploaded and the run is being triggered. */
  submitting = signal(false);
  /** Non-null when upload/trigger failed; surfaced as an error message. */
  submitError = signal<string | null>(null);

  /**
   * Upload the selected files to the session archive, then commit the run and
   * trigger the conversion workflow. File types are assigned in the UI before
   * processing, so files are uploaded here (at process time) rather than on
   * selection. Already-uploaded rows (with an ordinal) are skipped so a
   * re-process does not duplicate them. On success the progress dialog opens
   * and polls /api/progress for the new run.
   */
  private async submitProcessing(): Promise<void> {
    const token = this.state().tokenBase;
    if (!token || this.submitting()) return;
    this.submitError.set(null);
    this.submitting.set(true);
    // Tracks the operation in flight so a failure can name the exact step
    // (which file upload, or the conversion trigger) in the error message.
    let step = 'starting processing';
    try {
      const rows = this.rows();
      for (let i = 0; i < rows.length; i++) {
        const row = rows[i];
        if (!row.selected || row.ordinal != null || !row.fileType) continue;
        step = `uploading "${row.name}"`;
        const form = new FormData();
        form.append('token', token);
        form.append('file_type', row.fileType);
        form.append('file', row.file, row.name);
        const res = await firstValueFrom(
          this.http.post<{ ordinal: number }>(API_URL + 'upload', form),
        );
        // Tag the row with its server ordinal so re-processing skips it.
        this.rows.update((prev) =>
          prev.map((r, idx) => (idx === i ? { ...r, ordinal: res.ordinal } : r)),
        );
      }

      step = 'starting conversion';
      const res = await firstValueFrom(
        this.http.post<{ conversion_id: number; run_number: number }>(API_URL + 'process', {
          token,
        }),
      );
      this.pageService.pageState.update((prev) => ({ ...prev, conversionId: res.conversion_id }));
      this.submitting.set(false);
      this.openProgress();
    } catch (err) {
      this.submitting.set(false);
      const detail = this.describeHttpError(err);
      this.submitError.set(`Failed while ${step}: ${detail}`);
      console.error(`Processing failed while ${step}`, err);
    }
  }

  /**
   * Build a debugging-friendly description of a failed HTTP call: the backend's
   * JSON error message when present, otherwise the HTTP status, a connection
   * diagnosis for status 0, or a snippet of a non-JSON (e.g. HTML 500) body.
   */
  private describeHttpError(err: unknown): string {
    if (err instanceof TimeoutError) return 'the request timed out';
    if (err instanceof HttpErrorResponse) {
      const status = err.status ? `HTTP ${err.status} ${err.statusText}`.trim() : null;
      const body = err.error;
      // Backend errors are { error: '<message>' }; surface that as the primary text.
      if (body && typeof body === 'object' && typeof body.error === 'string') {
        return status ? `${body.error} (${status})` : body.error;
      }
      // Network/connection failure: no response was received at all.
      if (err.status === 0) {
        return 'could not reach the server (network or connection error)';
      }
      // Non-JSON response body (HTML error page, plain text): include a snippet.
      if (typeof body === 'string' && body.trim()) {
        const snippet = body.trim().slice(0, 300);
        return status ? `${status} — ${snippet}` : snippet;
      }
      return status ?? err.message ?? 'unknown HTTP error';
    }
    if (err instanceof Error) return err.message;
    return String(err);
  }

  // ── Processing progress dialog ───────────────────────────────────────────────
  processing = signal(false);
  progressTasks = signal<ProgressTask[]>([]);
  progressDone = signal(false);
  expandedTask = signal<string | null>(null);
  taskLog = signal<string>('');
  /** Outcome of the latest run, driving the previous-upload status banner. */
  previousStatus = signal<'processing' | 'success' | 'failed' | null>(null);
  /** True when the dialog was opened to inspect a previous run (via the
   * banner): suppresses the auto-navigate-on-success behaviour. */
  private inspecting = signal(false);
  private pollSub?: Subscription;
  private logSub?: Subscription;

  /** Classify a finished run from its task statuses: a task failure or a
   * blocking NMR report (report_status === 'Error') is a failure; otherwise
   * the run succeeded. */
  private computeOutcome(tasks: ProgressTask[]): 'success' | 'failed' {
    const nmr = tasks.find((t) => t.task === 'convert_nmr_data');
    const failed = tasks.some((t) => t.status === 'failed');
    const blocked = nmr?.report_status === 'Error';
    return failed || blocked ? 'failed' : 'success';
  }

  /** One-shot fetch of the latest run's status to drive the banner on load. */
  private checkPreviousStatus(): void {
    const token = this.state().tokenBase;
    if (!token || this.state().conversionId === null) return;
    this.http
      .get<{
        tasks: ProgressTask[];
        done: boolean;
      }>(API_URL + 'progress', { params: { token } })
      .subscribe({
        next: (res) => {
          const tasks = res.tasks ?? [];
          this.progressTasks.set(tasks);
          this.progressDone.set(res.done);
          this.previousStatus.set(res.done ? this.computeOutcome(tasks) : 'processing');
        },
        error: (err) => console.error('Failed to load previous status', err),
      });
  }

  /** Re-open the dialog to inspect the previous run's status and logs (from
   * the banner) without auto-navigating away on success. */
  reopenProgress(): void {
    this.openProgress(true);
  }

  /** Open the dialog and poll task progress (~2.5s) until all tasks finish.
   * When `inspect` is set the dialog stays open on success instead of
   * navigating to the summary (used when reviewing a previous run). */
  private openProgress(inspect = false): void {
    const token = this.state().tokenBase;
    if (!token) return;
    this.inspecting.set(inspect);
    this.processing.set(true);
    this.progressDone.set(false);
    this.expandedTask.set(null);
    this.taskLog.set('');
    this.pollSub?.unsubscribe();
    this.pollSub = timer(0, 2500)
      .pipe(
        switchMap(() =>
          this.http.get<{ tasks: ProgressTask[]; done: boolean }>(API_URL + 'progress', {
            params: { token },
          }),
        ),
      )
      .subscribe({
        next: (res) => {
          this.progressTasks.set(res.tasks ?? []);
          if (res.done) {
            this.progressDone.set(true);
            this.pollSub?.unsubscribe();
            const outcome = this.computeOutcome(res.tasks ?? []);
            this.previousStatus.set(outcome);
            // Genuine success → close and transfer to the Upload summary tab.
            // A failure / blocking report, or inspect mode → keep the dialog
            // open with the logs.
            if (outcome === 'success' && !this.inspecting()) {
              this.closeProgress();
              this.router.navigate(['/summary'], { queryParamsHandling: 'preserve' });
            }
          }
        },
        error: (err) => console.error('Failed to poll progress', err),
      });
  }

  /** Close the dialog and stop all polling. */
  closeProgress(): void {
    this.processing.set(false);
    this.inspecting.set(false);
    this.pollSub?.unsubscribe();
    this.logSub?.unsubscribe();
    // Leave the banner reflecting the finished run's outcome.
    if (this.progressDone()) {
      this.previousStatus.set(this.computeOutcome(this.progressTasks()));
    }
  }

  /** Expand/collapse a task's log; while expanded (and not done) tail it live. */
  toggleLog(task: string): void {
    this.logSub?.unsubscribe();
    if (this.expandedTask() === task) {
      this.expandedTask.set(null);
      return;
    }
    this.expandedTask.set(task);
    this.taskLog.set('');
    const token = this.state().tokenBase;
    if (!token) return;
    this.logSub = timer(0, 2500)
      .pipe(
        switchMap(() =>
          this.http.get<{ text: string }>(API_URL + 'log', { params: { token, task } }),
        ),
      )
      .subscribe({
        next: (res) => {
          this.taskLog.set(res.text || '');
          if (this.progressDone()) this.logSub?.unsubscribe();
        },
        error: (err) => console.error('Failed to fetch log', err),
      });
  }

  /** PrimeNG icon class for a workflow task status. */
  taskIcon(status: string | null): string {
    switch (status) {
      case 'completed':
        return 'pi pi-check-circle text-teal-500';
      case 'processing':
        return 'pi pi-spin pi-spinner text-surface-500';
      case 'failed':
      case 'aborted':
        return 'pi pi-times-circle text-red-500';
      default:
        return 'pi pi-clock text-surface-300';
    }
  }
}
