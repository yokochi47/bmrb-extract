import { Injectable, effect, signal } from '@angular/core';

export enum TargetDepSys {
  ONEDEP,
  BMRBDEP,
  REPL_CS,
}

export interface PageState {
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

  constructor() {
    effect(() => {
      const state = this.pageState();

      if (!this.initialized || !state) {
        this.initialized = true;
        return;
      }
    });
  }
}
