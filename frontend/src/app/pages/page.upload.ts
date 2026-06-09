import { Component, computed, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { HttpClient } from '@angular/common/http';
import { ButtonModule } from 'primeng/button';
import { CardModule } from 'primeng/card';
import { CheckboxModule } from 'primeng/checkbox';
import { InputTextModule } from 'primeng/inputtext';
import { MessageModule } from 'primeng/message';
import { RadioButtonModule } from 'primeng/radiobutton';
import { SelectModule } from 'primeng/select';
import { DividerModule } from 'primeng/divider';

import { PageService, TargetDepsys } from './page.service';
import { API_URL } from '../../site.config';

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
    InputTextModule,
    MessageModule,
    RadioButtonModule,
    SelectModule,
    DividerModule,
  ],
  templateUrl: './page.upload.html',
})
export class Upload {
  protected readonly TargetDepsys = TargetDepsys;

  private pageService = inject(PageService);
  private http = inject(HttpClient);
  readonly state = this.pageService.pageState;

  /** Hidden once conversion ID is issued. */
  showSetupSection = computed(() => this.state().conversionId === null);

  /** Choose Files is disabled after the session is locked or downloaded. */
  isLocked = computed(
    () => this.state().lockedSession || this.state().expiredSession || !this.state().firstUpload,
  );

  importBmrbEntry = signal(false);
  bmrbId = signal('');

  rows = signal<FileRow[]>([]);

  requirementsMet = computed(() => {
    const selected = this.rows().filter((r) => r.selected);
    return selected.length > 0 && selected.every((r) => r.fileType !== null);
  });

  readonly depSystemOptions = [
    {
      label:
        'OneDep (Conventional: coordinates, assigned chemical shifts, NMR restraints) or (Combined: coordinates, NMR unified data)',
      value: TargetDepsys.onedep,
    },
    {
      label:
        'OneDep (Replacing CS: coordinates processed by OneDep, NMR unified data processed by OneDep, correct assigned chemical shifts)',
      value: TargetDepsys.repl_cs,
    },
    {
      label: 'BMRBdep (assigned chemical shifts)',
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
      this.pageService.pageState.update((prev) => ({
        ...prev,
        relatedBmrbId: null,
        validBmrbId: false,
      }));
    }
    this.persistDepsys(value, this.state().relatedBmrbId);
  }

  onImportBmrbChange(value: boolean): void {
    this.importBmrbEntry.set(value);
    if (!value) {
      this.bmrbId.set('');
      this.pageService.pageState.update((prev) => ({
        ...prev,
        relatedBmrbId: null,
        validBmrbId: false,
      }));
      this.persistDepsys(this.state().targetDepsys, null);
    }
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
