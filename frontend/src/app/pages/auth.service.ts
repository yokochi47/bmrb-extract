import { Injectable, computed, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, tap } from 'rxjs';

import { API_URL } from '../../site.config';

/** Current auth state (from GET /api/auth/me or a login/verify response). */
export interface AuthState {
  authenticated: boolean;
  email?: string;
  role?: 'user' | 'annotator';
  csrf_token?: string;
  /** Annotator has not yet satisfied the TOTP second factor this session. */
  totp_required?: boolean;
  totp_enrolled?: boolean;
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
}

/**
 * Client for the passwordless-login + annotator/help-desk API. Holds the auth
 * state as signals; the CSRF token is read by authInterceptor for mutating
 * requests. All requests go out with credentials (the httpOnly session cookie).
 */
@Injectable({ providedIn: 'root' })
export class AuthService {
  private http = inject(HttpClient);

  /** Null until the first /me probe resolves. */
  state = signal<AuthState | null>(null);

  authenticated = computed(() => !!this.state()?.authenticated);
  role = computed(() => this.state()?.role ?? null);
  email = computed(() => this.state()?.email ?? null);
  csrfToken = computed(() => this.state()?.csrf_token ?? null);
  /** Full annotator (admin) authority = annotator role with TOTP satisfied. */
  isAdmin = computed(() => this.role() === 'annotator' && !this.state()?.totp_required);
  totpRequired = computed(() => !!this.state()?.totp_required);
  totpEnrolled = computed(() => !!this.state()?.totp_enrolled);

  constructor() {
    this.refresh().subscribe({ error: () => this.state.set({ authenticated: false }) });
  }

  refresh(): Observable<AuthState> {
    return this.http
      .get<AuthState>(API_URL + 'auth/me')
      .pipe(tap((s) => this.state.set(s ?? { authenticated: false })));
  }

  requestLogin(email: string): Observable<{ ok: boolean; message: string }> {
    return this.http.post<{ ok: boolean; message: string }>(API_URL + 'auth/request_login', {
      email,
    });
  }

  verify(c: string): Observable<AuthState> {
    return this.http
      .post<AuthState>(API_URL + 'auth/verify', { c })
      .pipe(tap((s) => this.state.set(s)));
  }

  enrollTotp(): Observable<{ otpauth_uri: string; qr: string }> {
    return this.http.post<{ otpauth_uri: string; qr: string }>(API_URL + 'auth/totp/enroll', {});
  }

  verifyTotp(code: string): Observable<AuthState> {
    return this.http
      .post<AuthState>(API_URL + 'auth/totp/verify', { code })
      .pipe(tap(() => this.refresh().subscribe()));
  }

  logout(): Observable<{ ok: boolean }> {
    return this.http
      .post<{ ok: boolean }>(API_URL + 'auth/logout', {})
      .pipe(tap(() => this.state.set({ authenticated: false })));
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

  postReply(conversionId: number, content: string): Observable<{ ok: boolean }> {
    return this.http.post<{ ok: boolean }>(API_URL + 'help/reply', {
      conversion_id: conversionId,
      content,
    });
  }
}
