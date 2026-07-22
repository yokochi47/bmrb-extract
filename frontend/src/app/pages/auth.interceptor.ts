import { inject } from '@angular/core';
import { HttpInterceptorFn } from '@angular/common/http';

import { AuthService } from './auth.service';
import { API_URL } from '../../site.config';

const MUTATING = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);

/**
 * Send the httpOnly auth-session cookie with every API request, and attach the
 * per-session CSRF token on state-changing requests. The token comes from
 * AuthService (populated by /api/auth/me and login); requests made before it is
 * known simply omit the header (the backend only enforces CSRF where a session
 * exists).
 */
export const authInterceptor: HttpInterceptorFn = (req, next) => {
  if (!req.url.startsWith(API_URL)) {
    return next(req);
  }
  let headers = req.headers;
  if (MUTATING.has(req.method)) {
    const csrf = inject(AuthService).csrfToken();
    if (csrf) {
      headers = headers.set('X-CSRF-Token', csrf);
    }
  }
  return next(req.clone({ withCredentials: true, headers }));
};
