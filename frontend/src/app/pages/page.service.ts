import { Injectable, effect, inject, signal } from '@angular/core';
import { Router } from '@angular/router';
// import { HttpClient } from '@angular/common/http';

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
  // private apiUrl = 'http://127.0.0.1:8000/api/';  // Flask API

  private router = inject(Router);
  // constructor(private http: HttpClient) {}

  constructor() {
    effect(() => {
      const state = this.pageState();

      console.log('current url: ', this.router.url);

      if (!this.initialized || !state) {
        this.initialized = true;
        return;
      }

      if (state.firstConsent && state.consentedTo) {
        console.log('First consent');
        this.pageState.update((prev) => ({
          ...prev,
          firstConsent: false,
        }));
      }
    });
  }
}
