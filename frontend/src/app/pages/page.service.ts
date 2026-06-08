import { Injectable, effect, inject, signal } from '@angular/core';
import { Router } from '@angular/router';
import { HttpClient } from '@angular/common/http';

import { API_URL } from '../../site.config';

export enum TargetDepSys {
  ONEDEP,
  BMRBDEP,
  REPL_CS,
}

export interface PageState {
  firstConsent: boolean;
  consentedTo: boolean;
  targetDepSys: TargetDepSys;
  referEntryId: string | null;
  validEntryId: boolean;
  firstUpload: boolean;
  lockedSession: boolean;
  expiredSession: boolean;
  signedIn: boolean;
  adminUser: boolean;
  tokenBase: string | null;
  conversionId: number | null;
}

@Injectable({
  providedIn: 'root',
})
export class PageService {
  /** Set to true by tokenGuard to trigger the consent-required dialog in AppLayout. */
  consentRequired = signal(false);

  /** Cached result of the most recent token DB validation. null = not yet checked. */
  tokenValidation = signal<'valid' | 'expired' | 'invalid' | null>(null);

  pageState = signal<PageState>({
    firstConsent: true,
    consentedTo: false,
    targetDepSys: TargetDepSys.ONEDEP,
    referEntryId: null,
    validEntryId: false,
    firstUpload: true,
    lockedSession: false,
    expiredSession: false,
    signedIn: false,
    adminUser: false,
    tokenBase: null,
    conversionId: null,
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
      }

      if (!state.firstConsent && state.consentedTo && state.tokenBase) {
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
        .get<{ conversion_id: number | null; expired: boolean }>(API_URL + 'session', { params: { token } })
        .subscribe({
          next: ({ conversion_id, expired }) => {
            if (expired) {
              this.tokenValidation.set('expired');
              this.pageState.update((prev) => ({ ...prev, expiredSession: true }));
            } else {
              this.tokenValidation.set('valid');
              this.pageState.update((prev) => ({ ...prev, conversionId: conversion_id }));
            }
          },
          error: () => {
            this.tokenValidation.set('invalid');
          },
        });
    }
  }

  private newConsent() {
    this.http.post<{ token: string }>(API_URL + 'new_consent', {}).subscribe({
      next: ({ token }) => {
        this.pageState.update((prev) => ({ ...prev, tokenBase: token }));
        this.router.navigate(['/info'], {
          queryParams: { token },
          replaceUrl: true,
        });
      },
      error: (err) => {
        console.error('Failed to obtain session token', err);
        this.pageState.update((prev) => ({ ...prev, firstConsent: true, consentedTo: false }));
      },
    });
  }
}
