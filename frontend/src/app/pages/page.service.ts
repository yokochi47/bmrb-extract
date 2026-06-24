import { Injectable, effect, inject, signal } from '@angular/core';
import { Router } from '@angular/router';
import { HttpClient } from '@angular/common/http';

import { API_URL } from '../../site.config';

export enum TargetDepsys {
  onedep,
  repl_cs,
  bmrbdep,
}

export interface PageState {
  firstConsent: boolean;
  consentedTo: boolean;
  targetDepsys: TargetDepsys;
  relatedBmrbId: number | null;
  firstUpload: boolean;
  lockedSession: boolean;
  expiredSession: boolean;
  signedIn: boolean;
  adminUser: boolean;
  tokenBase: string | null;
  conversionId: number | null;
  /** User has acknowledged all warnings (Terms #7) — gates download. */
  approved: boolean;
  /** Conversion results have been downloaded — session is read-only. */
  downloaded: boolean;
}

@Injectable({
  providedIn: 'root',
})
export class PageService {
  /** Set to true by tokenGuard to trigger the consent-required dialog in AppLayout. */
  consentRequired = signal(false);

  /** Non-null when starting a session (POST /api/new_consent) failed, so the
   * consent page can prompt the user to retry instead of silently unchecking. */
  consentError = signal<string | null>(null);

  /** Cached result of the most recent token DB validation. null = not yet checked. */
  tokenValidation = signal<'valid' | 'expired' | 'invalid' | null>(null);

  pageState = signal<PageState>({
    firstConsent: true,
    consentedTo: false,
    targetDepsys: TargetDepsys.onedep,
    relatedBmrbId: null,
    firstUpload: true,
    lockedSession: false,
    expiredSession: false,
    signedIn: false,
    adminUser: false,
    tokenBase: null,
    conversionId: null,
    approved: false,
    downloaded: false,
  });

  private initialized = false;
  private router = inject(Router);
  private http = inject(HttpClient);

  constructor() {
    effect(() => {
      const state = this.pageState();

      if (!this.initialized || !state) {
        this.initialized = true;
        return;
      }

      if (state.firstConsent && state.consentedTo && !state.tokenBase) {
        this.pageState.update((prev) => ({ ...prev, firstConsent: false }));
        this.newConsent();
      } else if (!state.firstConsent && state.consentedTo && state.tokenBase) {
        this.consentRequired.set(false);
      }
    });

    // Restore session from URL on page load / refresh
    const token = new URLSearchParams(window.location.search).get('token');
    if (token) {
      this.pageState.update((prev) => ({
        ...prev,
        tokenBase: token,
        firstConsent: false,
        consentedTo: true,
      }));
      this.http
        .get<{
          conversion_id: number | null;
          expired: boolean;
          target_depsys: string;
          related_bmrb_id: number | null;
          approved: boolean;
          downloaded: boolean;
        }>(API_URL + 'session', { params: { token } })
        .subscribe({
          next: ({
            conversion_id,
            expired,
            target_depsys,
            related_bmrb_id,
            approved,
            downloaded,
          }) => {
            if (expired) {
              this.tokenValidation.set('expired');
              this.pageState.update((prev) => ({ ...prev, expiredSession: true }));
            } else {
              this.tokenValidation.set('valid');
              this.pageState.update((prev) => ({
                ...prev,
                conversionId: conversion_id,
                targetDepsys:
                  TargetDepsys[target_depsys as keyof typeof TargetDepsys] ?? TargetDepsys.onedep,
                relatedBmrbId: related_bmrb_id,
                approved: !!approved,
                downloaded: !!downloaded,
              }));
            }
          },
          error: () => {
            this.tokenValidation.set('invalid');
          },
        });
    }
  }

  private newConsent() {
    this.consentError.set(null);
    this.http.post<{ token: string }>(API_URL + 'new_consent', {}).subscribe({
      next: ({ token }) => {
        this.consentError.set(null);
        this.pageState.update((prev) => ({ ...prev, tokenBase: token }));
        this.router.navigate(['/info'], {
          queryParams: { token },
          replaceUrl: true,
        });
      },
      error: (err) => {
        console.error('Failed to obtain session token', err);
        this.consentError.set(
          'Could not start a session — the server did not respond as expected. ' +
            'Please check the box again to retry; if it keeps failing, reload the page or contact us.',
        );
        this.pageState.update((prev) => ({ ...prev, firstConsent: true, consentedTo: false }));
      },
    });
  }
}
