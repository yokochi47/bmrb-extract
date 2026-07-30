import { Injectable, computed, effect, inject, signal } from '@angular/core';
import { NavigationEnd, Router } from '@angular/router';
import { HttpClient } from '@angular/common/http';
import { filter } from 'rxjs';

import { API_URL } from '../../site.config';
import { AuthService } from './auth.service';

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
  /** Date (YYYY-MM-DD) the session and its results stay accessible; null until
   * the session is restored from the backend. */
  tokenExpiry: string | null;
  /** User has acknowledged all warnings (Terms #7) — gates download. */
  approved: boolean;
  /** Conversion results have been downloaded — session is read-only. */
  downloaded: boolean;
  /** The logged-in user already owns this session. */
  owned: boolean;
  /** A logged-in user holding this token may adopt this (unowned, non-expired)
   * session into their account. */
  claimable: boolean;
  /** Session lifecycle status ('completed' | 'failed' | 'processing' | …); null
   * until restored. 'failed' ⇒ the run did not complete, results not downloadable. */
  sessionStatus: string | null;
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
    tokenExpiry: null,
    approved: false,
    downloaded: false,
    owned: false,
    claimable: false,
    sessionStatus: null,
  });

  /** Single source of truth for "the run did not complete" — a conversion task
   * failed/aborted or a blocking NMR report; the backend sets session.status =
   * 'failed' for all of these. Shared by the summary and download pages so they
   * never diverge. Its results are incomplete and must not be downloaded. */
  sessionFailed = computed(() => this.pageState().sessionStatus === 'failed');

  private initialized = false;
  /** Token whose session state is currently loaded, so navigation events don't
   * re-fetch the same session repeatedly. */
  private loadedToken: string | null = null;
  private router = inject(Router);
  private http = inject(HttpClient);
  private auth = inject(AuthService);

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

    // Restore session state whenever a token is present in the URL: at initial
    // page load AND on SPA navigation to a token-bearing route (e.g. opening a
    // session from "My sessions", a resume link). Constructor-only restore used
    // to miss SPA navigations, leaving the target page without session state.
    this.restoreSession(new URLSearchParams(window.location.search).get('token'));
    this.router.events
      .pipe(filter((e): e is NavigationEnd => e instanceof NavigationEnd))
      .subscribe(() =>
        this.restoreSession(new URLSearchParams(window.location.search).get('token')),
      );
  }

  /** Fetch and restore session state for `token`. No-op if the token is absent
   * or already loaded. Called at page load and on navigation to a token route. */
  private restoreSession(token: string | null): void {
    if (!token || token === this.loadedToken) return;
    this.loadedToken = token;
    // consentedTo is restored from the backend below (not forced true), so a
    // revoked consent stays revoked across reloads / direct URLs.
    this.pageState.update((prev) => ({ ...prev, tokenBase: token, firstConsent: false }));
    this.http
      .get<{
        conversion_id: number | null;
        expired: boolean;
        token_expiry: string;
        consented: boolean;
        target_depsys: string;
        related_bmrb_id: number | null;
        approved: boolean;
        downloaded: boolean;
        owned: boolean;
        claimable: boolean;
        status: string;
      }>(API_URL + 'session', { params: { token } })
      .subscribe({
        next: ({
          conversion_id,
          expired,
          token_expiry,
          consented,
          target_depsys,
          related_bmrb_id,
          approved,
          downloaded,
          owned,
          claimable,
          status,
        }) => {
          if (expired) {
            this.tokenValidation.set('expired');
            this.pageState.update((prev) => ({ ...prev, expiredSession: true }));
          } else {
            this.tokenValidation.set('valid');
            // Remember this session so it can be adopted if the user logs in
            // (e.g. clicks Login before saving the resume URL). Harmless if the
            // session is already owned — the backend claim is idempotent/guarded.
            this.auth.rememberPendingClaim(token);
            this.pageState.update((prev) => ({
              ...prev,
              consentedTo: !!consented,
              conversionId: conversion_id,
              tokenExpiry: token_expiry,
              targetDepsys:
                TargetDepsys[target_depsys as keyof typeof TargetDepsys] ?? TargetDepsys.onedep,
              relatedBmrbId: related_bmrb_id,
              approved: !!approved,
              downloaded: !!downloaded,
              owned: !!owned,
              claimable: !!claimable,
              sessionStatus: status ?? null,
            }));
          }
        },
        error: () => {
          this.tokenValidation.set('invalid');
        },
      });
  }

  /** Mark the current session as adopted into the logged-in user's account
   * (after a successful claim), so the claim affordance disappears. */
  markSessionClaimed(): void {
    this.pageState.update((prev) => ({ ...prev, owned: true, claimable: false }));
  }

  /** Re-fetch ownership / claimability / lifecycle status for the current session.
   * `claimable` is auth-dependent and `sessionStatus` becomes terminal only after
   * processing, so both must be refreshed after login / a claim / reaching the
   * summary — the initial (pre-processing, anonymous) values are otherwise stale
   * until a manual reload (same-token navigation skips a full restore). */
  refreshSession(token?: string | null): void {
    const t = token ?? this.pageState().tokenBase;
    if (!t) return;
    this.http
      .get<{ owned: boolean; claimable: boolean; status: string }>(API_URL + 'session', {
        params: { token: t },
      })
      .subscribe({
        next: ({ owned, claimable, status }) =>
          this.pageState.update((prev) => ({
            ...prev,
            owned: !!owned,
            claimable: !!claimable,
            sessionStatus: status ?? null,
          })),
        error: () => undefined,
      });
  }

  /** Toggle consent. Updates the in-memory flag, and for an existing session
   * (token present) persists it via POST /api/consent so a revoked consent is
   * enforced on reload / direct URL. The first-ever consent (no token yet) is
   * handled by the effect above, which calls newConsent() to create the session
   * with consented=true. */
  setConsent(consented: boolean) {
    this.pageState.update((prev) => ({ ...prev, consentedTo: consented }));
    const token = this.pageState().tokenBase;
    if (!token) return;
    this.http.post(API_URL + 'consent', { token, consented }).subscribe({
      error: (err) => console.error('Failed to update consent', err),
    });
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
