import { Injectable, computed, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, filter, map, race, take, tap, timer } from 'rxjs';

import { API_URL } from '../../site.config';
import { AuthBroadcast, AuthChannel } from './auth-channel';

/** Current auth state (from GET /api/auth/me or a login/verify response). */
export interface AuthState {
  authenticated: boolean;
  email?: string;
  role?: 'user' | 'annotator';
  csrf_token?: string;
  /** Annotator has not yet satisfied the TOTP second factor this session. */
  totp_required?: boolean;
  totp_enrolled?: boolean;
  /** The session bound to the login challenge was adopted on verify. */
  claimed_session?: boolean;
}

/** Response to POST /api/auth/request_login. Deliberately identical for every
 * address (valid, unknown or malformed), so it reveals nothing. */
export interface LoginRequested {
  ok: boolean;
  message: string;
  /** Handle for the emailed code; also echoed in the link as ?p=. */
  pending_id: string;
  /** Seconds the link and code remain valid. */
  expires_in: number;
}

/** localStorage key holding the token of the session to adopt once the user logs
 * in — set while viewing a session, read when requesting a login link, so a
 * pending claim survives the login round-trip (even a cross-device magic link). */
const PENDING_CLAIM_KEY = 'bmrbx_pending_claim';

/** localStorage key holding the login this browser is waiting to complete with the
 * emailed code. Persisted rather than kept in memory so a reload does not lose the
 * wait, and read by page.login-verify to tell the browser that asked for the link
 * from a browser that merely opened the mail. */
const PENDING_LOGIN_KEY = 'bmrbx_pending_login';

/** The sign-in mail this browser is waiting on. */
export interface PendingLogin {
  /** Opaque handle from POST /api/auth/request_login; also carried in the link. */
  pending_id: string;
  email: string;
  /** Epoch ms. A stash past this is treated as absent. */
  expires_at: number;
}

/** One of the caller's sessions (GET /api/sessions). */
export interface SessionRow {
  conversion_id: number | null;
  public_id: string | null;
  status: string;
  target_depsys: string;
  created_at: string | null;
  token_expiry: string | null;
  approved: boolean;
  downloaded: boolean;
  /** Present for own sessions (reopen link). */
  token?: string;
  /** Present for annotator scope=all rows (audited admin open). */
  token_admin?: string;
}

export interface Inquiry {
  conversion_id: number;
  public_id: string;
  ordinal: number;
  subject: string;
  content: string;
  email_address: string;
  sent_at: string | null;
  /** true = annotator reply, false = the user's own inquiry. */
  from_admin?: boolean;
}

/**
 * Client for the passwordless-login + annotator/help-desk API. Holds the auth
 * state as signals; the CSRF token is read by authInterceptor for mutating
 * requests. All requests go out with credentials (the httpOnly session cookie).
 */
@Injectable({ providedIn: 'root' })
export class AuthService {
  private http = inject(HttpClient);
  private channel = inject(AuthChannel);

  /** Null until the first /me probe resolves. */
  state = signal<AuthState | null>(null);

  /** Bumped after each claim (auto or manual) so views can re-check ownership. */
  claimRevision = signal(0);

  /** Help-desk notification state (per site). For an annotator: sessions awaiting
   * a reply; for a user: own sessions with a new (unseen) annotator reply. */
  unread = signal<{ count: number; conversion_ids: number[] }>({ count: 0, conversion_ids: [] });
  unreadCount = computed(() => this.unread().count);
  unreadIds = computed(() => new Set(this.unread().conversion_ids));

  authenticated = computed(() => !!this.state()?.authenticated);
  role = computed(() => this.state()?.role ?? null);
  email = computed(() => this.state()?.email ?? null);
  csrfToken = computed(() => this.state()?.csrf_token ?? null);
  /** Full annotator (admin) authority = annotator role with TOTP satisfied. */
  isAdmin = computed(() => this.role() === 'annotator' && !this.state()?.totp_required);
  totpRequired = computed(() => !!this.state()?.totp_required);
  totpEnrolled = computed(() => !!this.state()?.totp_enrolled);

  /** True while this tab waits for the emailed sign-in link. It marks the tab as
   * the one the user started from, so it — and only it — acknowledges the login
   * announced by the tab the mail application opened. */
  awaitingMagicLink = signal(false);

  /** Bumped once a login performed in another tab has been picked up here. */
  loginElsewhere = signal(0);

  /** The sign-in mail this browser is waiting on, or null. Restored from
   * localStorage, so it outlives a reload of the tab that started the login. */
  pendingLogin = signal<PendingLogin | null>(this.readPendingLogin());

  constructor() {
    this.refresh().subscribe({ error: () => this.state.set({ authenticated: false }) });
    // A login is still outstanding in this browser (we reloaded, or came back to
    // the page): re-arm the hand-back so a link opened in another tab is caught.
    if (this.pendingLogin()) this.awaitingMagicLink.set(true);
    // Poll help-desk notifications for the bell badge (per site).
    timer(60000, 60000).subscribe(() => this.refreshUnread());
    this.channel.messages.subscribe((m) => this.onBroadcast(m));
  }

  /** Absorb an auth event from another tab of this browser. */
  private onBroadcast(m: AuthBroadcast): void {
    if (m.type === 'logout') {
      // The cookie is already gone origin-wide; just drop the local state.
      this.state.set({ authenticated: false });
      this.unread.set({ count: 0, conversion_ids: [] });
      return;
    }
    if (m.type !== 'login') return;
    // Acknowledge first, before the round-trip, so the announcing tab does not
    // time out waiting for us.
    if (this.awaitingMagicLink()) this.channel.post({ type: 'login-ack' });
    // The session cookie was set for the whole origin — /me now succeeds here too.
    this.refresh().subscribe({
      next: () => this.loginElsewhere.update((v) => v + 1),
      error: () => undefined,
    });
  }

  /** Wait (briefly) for another tab to claim the login this tab just performed.
   * Emits false once the window elapses with no acknowledgement. */
  awaitLoginAck(ms: number): Observable<boolean> {
    return race(
      this.channel.messages.pipe(
        filter((m) => m.type === 'login-ack'),
        map(() => true),
      ),
      timer(ms).pipe(map(() => false)),
    ).pipe(take(1));
  }

  refresh(): Observable<AuthState> {
    return this.http.get<AuthState>(API_URL + 'auth/me').pipe(
      tap((s) => {
        this.state.set(s ?? { authenticated: false });
        // Auto-claim: once we know the user is logged in, silently adopt the
        // anonymous session whose token is in the URL (if any).
        if (s?.authenticated) {
          this.autoClaimFromUrl();
          this.refreshUnread();
        } else {
          this.unread.set({ count: 0, conversion_ids: [] });
        }
      }),
    );
  }

  /** Refresh the help-desk notification counts (bell badge + row highlights). */
  refreshUnread(): void {
    if (!this.authenticated()) {
      this.unread.set({ count: 0, conversion_ids: [] });
      return;
    }
    this.http.get<{ count: number; conversion_ids: number[] }>(API_URL + 'help/unread').subscribe({
      next: (r) => this.unread.set(r ?? { count: 0, conversion_ids: [] }),
      error: () => undefined,
    });
  }

  /** Bind the current account to a session started anonymously. Requires the
   * caller to hold the session's `token`; only unowned, non-expired sessions are
   * claimed. Idempotent (already_owned) for the owner. */
  claimSession(token: string): Observable<{ ok: boolean; already_owned?: boolean }> {
    return this.http
      .post<{ ok: boolean; already_owned?: boolean }>(API_URL + 'auth/claim_session', { token })
      .pipe(tap(() => this.claimRevision.update((v) => v + 1)));
  }

  /** After login, silently adopt the anonymous session referenced by ?token= in
   * the URL. No-op / ignored error when absent, already owned, expired, or owned
   * by another account (the backend enforces all of these). */
  private autoClaimFromUrl(): void {
    let token: string | null = null;
    try {
      token = new URLSearchParams(window.location.search).get('token');
    } catch {
      // Malformed URL — nothing to claim.
    }
    if (token) this.claimSession(token).subscribe({ error: () => undefined });
  }

  requestLogin(
    email: string,
    claimToken?: string | null,
  ): Observable<LoginRequested> {
    const body: { email: string; claim_token?: string } = { email };
    if (claimToken) body.claim_token = claimToken;
    return this.http.post<LoginRequested>(API_URL + 'auth/request_login', body).pipe(
      tap((r) => {
        if (!r?.pending_id) return;
        this.writePendingLogin({
          pending_id: r.pending_id,
          email,
          expires_at: Date.now() + (r.expires_in ?? 900) * 1000,
        });
      }),
    );
  }

  /** Consume the emailed link. `crossDevice` says this is not the browser that
   * asked for it, which the backend refuses for annotators (their second factor
   * belongs on the device they started from) — a 403 leaves the link unused. */
  verify(c: string, crossDevice = false): Observable<AuthState> {
    const body: { c: string; cross_device?: boolean } = { c };
    if (crossDevice) body.cross_device = true;
    return this.http
      .post<AuthState>(API_URL + 'auth/verify', body)
      .pipe(tap((s) => this.applyLogin(s)));
  }

  /** Consume the emailed code. The cookie is set on this response, so the login
   * lands on THIS device — the one that asked for the mail. */
  verifyCode(pendingId: string, code: string): Observable<AuthState> {
    return this.http
      .post<AuthState>(API_URL + 'auth/verify_code', { pending_id: pendingId, code })
      .pipe(tap((s) => this.applyLogin(s)));
  }

  /** Adopt a completed login, whichever credential produced it. Mirrors the
   * backend's _establish_login so both entry points leave the client identical. */
  private applyLogin(s: AuthState): void {
    this.state.set(s);
    // The pending claim (if any) was bound to the challenge and resolved
    // server-side on verify; drop the local stash so it isn't reused.
    this.clearPendingClaim();
    // This challenge is spent — neither the link nor the code can be used again.
    this.clearPendingLogin();
    // Tell the tab the user started from — the cookie is now valid there too.
    this.channel.post({ type: 'login', totp_required: !!s.totp_required });
  }

  // --- pending-login stash (the sign-in mail this browser is waiting on) ---- //

  private readPendingLogin(): PendingLogin | null {
    try {
      const raw = localStorage.getItem(PENDING_LOGIN_KEY);
      if (!raw) return null;
      const p = JSON.parse(raw) as PendingLogin;
      if (!p?.pending_id || !(p.expires_at > Date.now())) {
        localStorage.removeItem(PENDING_LOGIN_KEY);
        return null;
      }
      return p;
    } catch {
      return null; // storage unavailable or malformed — behave as if absent.
    }
  }

  private writePendingLogin(p: PendingLogin): void {
    this.pendingLogin.set(p);
    try {
      localStorage.setItem(PENDING_LOGIN_KEY, JSON.stringify(p));
    } catch {
      // storage unavailable (private mode / disabled) — the in-memory signal
      // still carries this tab through, it just will not survive a reload.
    }
  }

  clearPendingLogin(): void {
    this.pendingLogin.set(null);
    try {
      localStorage.removeItem(PENDING_LOGIN_KEY);
    } catch {
      // ignore
    }
  }

  // --- pending-claim stash (survives the login round-trip) ------------------ //

  /** Remember the session the user is currently viewing, to adopt it on login. */
  rememberPendingClaim(token: string | null | undefined): void {
    try {
      if (token) localStorage.setItem(PENDING_CLAIM_KEY, token);
    } catch {
      // storage unavailable (private mode / disabled) — degrade gracefully.
    }
  }

  /** Token to adopt on login: the ?token= in the URL if present, else the stash. */
  pendingClaimToken(): string | null {
    try {
      const fromUrl = new URLSearchParams(window.location.search).get('token');
      if (fromUrl) return fromUrl;
      return localStorage.getItem(PENDING_CLAIM_KEY);
    } catch {
      return null;
    }
  }

  clearPendingClaim(): void {
    try {
      localStorage.removeItem(PENDING_CLAIM_KEY);
    } catch {
      // ignore
    }
  }

  enrollTotp(): Observable<{ otpauth_uri: string; qr: string }> {
    return this.http.post<{ otpauth_uri: string; qr: string }>(API_URL + 'auth/totp/enroll', {});
  }

  verifyTotp(code: string): Observable<AuthState> {
    return this.http.post<AuthState>(API_URL + 'auth/totp/verify', { code }).pipe(
      tap(() => {
        this.refresh().subscribe();
        // Other tabs gain admin authority at the same moment — let them re-read it.
        this.channel.post({ type: 'login', totp_required: false });
      }),
    );
  }

  logout(): Observable<{ ok: boolean }> {
    return this.http.post<{ ok: boolean }>(API_URL + 'auth/logout', {}).pipe(
      tap(() => {
        this.state.set({ authenticated: false });
        this.unread.set({ count: 0, conversion_ids: [] });
        this.channel.post({ type: 'logout' });
      }),
    );
  }

  listSessions(scope?: 'all'): Observable<{ sessions: SessionRow[]; scope: string }> {
    return this.http.get<{ sessions: SessionRow[]; scope: string }>(API_URL + 'sessions', {
      params: scope ? { scope } : {},
    });
  }

  postInquiry(token: string, subject: string, content: string): Observable<{ ok: boolean }> {
    return this.http.post<{ ok: boolean }>(API_URL + 'help/inquiry', { token, subject, content });
  }

  getInquiries(): Observable<{ inquiries: Inquiry[] }> {
    return this.http.get<{ inquiries: Inquiry[] }>(API_URL + 'help/inquiries');
  }

  /** The message thread (own inquiries + annotator replies) for one of the
   * caller's own sessions, newest first. */
  getThread(token: string): Observable<{ messages: Inquiry[] }> {
    return this.http.get<{ messages: Inquiry[] }>(API_URL + 'help/thread', { params: { token } });
  }

  postReply(conversionId: number, content: string): Observable<{ ok: boolean }> {
    return this.http.post<{ ok: boolean }>(API_URL + 'help/reply', {
      conversion_id: conversionId,
      content,
    });
  }
}
