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

/** Human-readable labels for the OutputFileType values (GET /api/output_files). */
const OUTPUT_TYPE_LABELS: Record<string, string> = {
  pdbx: 'Coordinates (PDBx/mmCIF)',
  'nmr-star': 'NMR data (NMR-STAR)',
  nef: 'NMR data (NEF)',
  text_report: 'Conversion report (text)',
  json_report: 'Validation report (JSON)',
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
  }

  /** Conversion result files bundled in the zip (GET /api/output_files). */
  files = signal<OutputFileRow[]>([]);

  /** The deferred NMR-STAR→NEF release is still running (poll until it clears). */
  nefGenerating = signal(false);

  /** A NEF file was produced — else the table shows the NEF-unavailable note. */
  hasNef = computed(() => this.files().some((f) => f.file_type === 'nef'));

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

  /** Format a byte count as a compact size string. */
  formatSize(bytes: number): string {
    if (!bytes) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(1024));
    return `${(bytes / Math.pow(1024, i)).toFixed(1)} ${units[i]}`;
  }
}
