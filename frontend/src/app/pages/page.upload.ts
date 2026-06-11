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

  requirementsMet = computed(() => {
    const selected = this.rows().filter((r) => r.selected);
    return selected.length > 0 && selected.every((r) => r.fileType !== null);
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
    { label: 'Coordinate (mmCIF/PDBx format)', value: 'co-cif' },
    { label: 'Coordinate (legacy PDB format)', value: 'co-pdb' },
    // NMR unified
    { label: 'NMR unified data (NMR-STAR format)', value: 'nm-uni-str' },
    { label: 'NMR unified data (NEF format)', value: 'nm-uni-nef' },
    // Chemical shifts
    { label: 'Assigned chemical shifts (any format)', value: 'nm-shi' },
    { label: 'Assigned chemical shifts — ARIA', value: 'nm-shi-ari' },
    { label: 'Assigned chemical shifts — BARDISC', value: 'nm-shi-bar' },
    { label: 'Assigned chemical shifts — GARFIELD', value: 'nm-shi-gar' },
    { label: 'Assigned chemical shifts — NMRPipe', value: 'nm-shi-npi' },
    { label: 'Assigned chemical shifts — OLIVIA', value: 'nm-shi-oli' },
    { label: 'Assigned chemical shifts — PIPP', value: 'nm-shi-pip' },
    { label: 'Assigned chemical shifts — PPM', value: 'nm-shi-ppm' },
    { label: 'Assigned chemical shifts — SPARKY/STAR2', value: 'nm-shi-st2' },
    { label: 'Assigned chemical shifts — XEASY/CYANA', value: 'nm-shi-xea' },
    // Spectral peak lists
    { label: 'Spectral peak list (any format)', value: 'nm-pea-any' },
    { label: 'Spectral peak list — ARIA', value: 'nm-pea-ari' },
    { label: 'Spectral peak list — BARDISC', value: 'nm-pea-bar' },
    { label: 'Spectral peak list — CCPN', value: 'nm-pea-ccp' },
    { label: 'Spectral peak list — OLIVIA', value: 'nm-pea-oli' },
    { label: 'Spectral peak list — PIPP', value: 'nm-pea-pip' },
    { label: 'Spectral peak list — PONDUS', value: 'nm-pea-pon' },
    { label: 'Spectral peak list — Sparky', value: 'nm-pea-spa' },
    { label: 'Spectral peak list — SPARKY-SPS', value: 'nm-pea-sps' },
    { label: 'Spectral peak list — TOPSPIN', value: 'nm-pea-top' },
    { label: 'Spectral peak list — VIENNA', value: 'nm-pea-vie' },
    { label: 'Spectral peak list — VNMR', value: 'nm-pea-vnm' },
    { label: 'Spectral peak list — XEASY/CYANA', value: 'nm-pea-xea' },
    { label: 'Spectral peak list — XWINNMR', value: 'nm-pea-xwi' },
    // Restraints
    { label: 'Restraint file — AMBER', value: 'nm-res-amb' },
    { label: 'Restraint file — ARIA', value: 'nm-res-ari' },
    { label: 'Restraint file — ARIA/X-PLOR', value: 'nm-res-arx' },
    { label: 'Restraint file — BARDISC', value: 'nm-res-bar' },
    { label: 'Restraint file — BIOSYM', value: 'nm-res-bio' },
    { label: 'Restraint file — CHARMM', value: 'nm-res-cha' },
    { label: 'Restraint file — CNS/X-PLOR', value: 'nm-res-cns' },
    { label: 'Restraint file — CYANA', value: 'nm-res-cya' },
    { label: 'Restraint file — DYNAMO', value: 'nm-res-dyn' },
    { label: 'Restraint file — GROMACS', value: 'nm-res-gro' },
    { label: 'Restraint file — ISD', value: 'nm-res-isd' },
    { label: 'Restraint file — NOA', value: 'nm-res-noa' },
    { label: 'Restraint file — other', value: 'nm-res-oth' },
    { label: 'Restraint file — ROSETTA', value: 'nm-res-ros' },
    { label: 'Restraint file — SAXS', value: 'nm-res-sax' },
    { label: 'Restraint file — SCHIMP', value: 'nm-res-sch' },
    { label: 'Restraint file — SYBYL', value: 'nm-res-syb' },
    { label: 'Restraint file — X-PLOR', value: 'nm-res-xpl' },
    // Auxiliary
    { label: 'Auxiliary — AMBER topology/parameter', value: 'nm-aux-amb' },
    { label: 'Auxiliary — CHARMM topology/parameter', value: 'nm-aux-cha' },
    { label: 'Auxiliary — GROMACS topology', value: 'nm-aux-gro' },
    { label: 'Auxiliary — PDB coordinates', value: 'nm-aux-pdb' },
    { label: 'Auxiliary — XEASY/CYANA sequence', value: 'nm-aux-xea' },
  ];

  setTargetDepsys(value: TargetDepsys): void {
    this.pageService.pageState.update((prev) => ({ ...prev, targetDepsys: value }));
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
