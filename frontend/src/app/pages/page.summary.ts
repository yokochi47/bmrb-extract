import { Component, computed, effect, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { CardModule } from 'primeng/card';
import { TableModule } from 'primeng/table';

import { PageService } from './page.service';
import { API_URL } from '../../site.config';
import { fileTypeLabel } from './file-types';

/** A selected upload file participating in the latest conversion run. */
interface UploadFileRow {
  original_name: string;
  file_size: number;
  file_type: string;
  source: string;
  /** Upload time as a naive UTC string ("YYYY-MM-DD HH:mm"); see GET /api/files. */
  uploaded_at: string | null;
}

@Component({
  selector: 'app-summary',
  imports: [CardModule, TableModule],
  templateUrl: './page.summary.html',
})
export class Summary {
  private pageService = inject(PageService);
  private http = inject(HttpClient);

  /** Selected files of the latest run, ordered by upload time (server-side). */
  files = signal<UploadFileRow[]>([]);

  /** The Source column is shown only when at least one file did not come from
   * the user (i.e. was downloaded from BMRB). */
  showSource = computed(() => this.files().some((f) => f.source !== 'user'));

  private fetched = false;

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
  }

  private loadFiles(token: string): void {
    this.http
      .get<{ files: UploadFileRow[] }>(API_URL + 'files', { params: { token } })
      .subscribe({
        next: (res) => this.files.set(res.files ?? []),
        error: (err) => console.error('Failed to load upload files', err),
      });
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
