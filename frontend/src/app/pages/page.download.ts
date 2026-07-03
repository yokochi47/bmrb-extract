import { Component, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { CardModule } from 'primeng/card';
import { ButtonModule } from 'primeng/button';

import { PageService } from './page.service';
import { API_URL } from '../../site.config';

/**
 * Download page (Terms #7) — see the "Download" UI mockup: a tokenized resume
 * URL with its expiry date, an optional email of that URL, the conversion
 * results zip, a table of its file contents, and the human-readable conversion
 * report/statistics.
 *
 * Scaffold: the resume URL, public id and zip name are wired from session
 * state; the expiry date, email send, zip stream, file-contents table and
 * report still need backend endpoints (each marked TODO(backend) below).
 */
@Component({
  selector: 'app-download',
  imports: [FormsModule, CardModule, ButtonModule],
  templateUrl: './page.download.html',
})
export class Download {
  private pageService = inject(PageService);

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

  /** Direct link to the conversion-results zip. */
  // TODO(backend): implement GET /api/download?token=… to stream the zip
  // (and record that the user downloaded).
  zipUrl = computed(() => {
    const token = this.pageService.pageState().tokenBase;
    return token ? `${API_URL}download?token=${encodeURIComponent(token)}` : '';
  });

  /** True once the results were downloaded — the session becomes read-only. */
  downloaded = computed(() => this.pageService.pageState().downloaded);

  /** Date (YYYY-MM-DD) until which the session and results stay accessible. */
  // TODO(backend): expose the session expiry date (e.g. from GET /api/session).
  expiryDate = signal<string | null>(null);

  /** Optional recipient for the resume URL. */
  email = signal('');
  emailSent = signal(false);

  /** Email the resume URL to the user (see the Communication tab). */
  sendResumeUrl(): void {
    // TODO(backend): POST the email + resume URL to a notification endpoint,
    // validate the address, and record it as a communication.
    if (!this.email().trim()) return;
    this.emailSent.set(true);
  }

  /** Download the results zip, then flip the session to read-only. */
  download(): void {
    const url = this.zipUrl();
    if (!url) return;
    window.location.href = url;
    // TODO(backend): persist downloaded=true so the read-only state (no further
    // re-upload) survives a reload and is enforced server-side.
    this.pageService.pageState.update((prev) => ({ ...prev, downloaded: true }));
  }
}
