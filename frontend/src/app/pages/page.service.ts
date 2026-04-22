import { Injectable, effect, inject, signal } from '@angular/core';
import { Router } from '@angular/router';
import { HttpClient } from '@angular/common/http';

import { FLASK_API_URL } from '../../site.config';

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
}

@Injectable({
  providedIn: 'root',
})
export class PageService {
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

      if (state.firstConsent && state.consentedTo && this.router.url == '/info') {
        this.pageState.update((prev) => ({
          ...prev,
          firstConsent: false,
        }));

        this.newToken();
      }
    });
  }

  private newToken() {
    console.log('Send request for new token');

    this.http.get(FLASK_API_URL + 'new_token');
  }
}
