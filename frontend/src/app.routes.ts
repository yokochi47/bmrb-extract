import { inject } from '@angular/core';
import { CanActivateFn, Router, Routes } from '@angular/router';
import { HttpClient } from '@angular/common/http';
import { catchError, map, of } from 'rxjs';

import { AppLayout } from './app/layout/app.layout';
import { PageService } from './app/pages/page.service';
import { AuthService } from './app/pages/auth.service';
import { API_URL } from './site.config';

/** Require a logged-in user; otherwise redirect to /login. */
export const authGuard: CanActivateFn = () => {
  const auth = inject(AuthService);
  const router = inject(Router);
  if (auth.authenticated()) {
    return true;
  }
  return auth.refresh().pipe(
    map((s) => (s.authenticated ? true : router.createUrlTree(['/login']))),
    catchError(() => of(router.createUrlTree(['/login']))),
  );
};

/**
 * Block navigation when the session token is absent, consent is revoked,
 * or the token is no longer valid in the DB. Shows a dialog in AppLayout
 * instead of silently redirecting.
 */
export const tokenGuard: CanActivateFn = (route) => {
  const pageService = inject(PageService);
  const router = inject(Router);
  const token = route.queryParamMap.get('token');

  if (!token) {
    // Returning to a session page after visiting a communication page (My sessions
    // / Help desk / Account) or the bell icon, which drop ?token= from the URL.
    // Re-attach the active session's token (held in memory) and redirect, rather
    // than demanding consent again for an already-consented session.
    const active = pageService.pageState().tokenBase;
    if (active) {
      const path = '/' + route.url.map((s) => s.path).join('/');
      return router.createUrlTree([path], {
        queryParams: { ...route.queryParams, token: active },
      });
    }
    pageService.consentRequired.set(true);
    return false;
  }

  // Fast path: reuse cached token-validity result from a prior HTTP call. Consent
  // is read live (consentedTo) so an in-session uncheck blocks immediately.
  const cached = pageService.tokenValidation();
  switch (cached) {
    case 'valid':
      if (!pageService.pageState().consentedTo) {
        pageService.consentRequired.set(true);
        return false;
      }
      return true;
    case 'expired':
      pageService.pageState.update((prev) => ({ ...prev, expiredSession: true }));
      return false;
    case 'invalid':
      pageService.consentRequired.set(true);
      return false;
  }

  // Slow path: validate against DB (tokenValidation is null — first navigation /
  // fresh reload). Consent comes from the backend here so a revoked consent is
  // enforced even though consentedTo starts false on a fresh page load.
  return inject(HttpClient)
    .get<{ conversion_id: number | null; expired: boolean; consented: boolean }>(
      API_URL + 'session',
      { params: { token } },
    )
    .pipe(
      map(({ conversion_id, expired, consented }) => {
        if (expired) {
          pageService.tokenValidation.set('expired');
          pageService.pageState.update((prev) => ({ ...prev, expiredSession: true }));
          return false;
        }
        pageService.tokenValidation.set('valid');
        pageService.pageState.update((prev) => ({
          ...prev,
          conversionId: conversion_id,
          consentedTo: !!consented,
        }));
        if (!consented) {
          pageService.consentRequired.set(true);
          return false;
        }
        return true;
      }),
      catchError(() => {
        pageService.tokenValidation.set('invalid');
        pageService.consentRequired.set(true);
        return of(false);
      }),
    );
};

export const appRoutes: Routes = [
  {
    path: '',
    component: AppLayout,
    children: [
      { path: '', redirectTo: 'info', pathMatch: 'full' },
      { path: 'info', loadComponent: () => import('./app/pages/page.info').then((m) => m.Info) },
      {
        path: 'preface',
        loadComponent: () => import('./app/pages/page.preface').then((m) => m.Preface),
      },
      { path: 'terms', loadComponent: () => import('./app/pages/page.terms').then((m) => m.Terms) },
      {
        path: 'privacy',
        loadComponent: () => import('./app/pages/page.privacy').then((m) => m.Privacy),
      },
      // Protected routes — require a valid session token in the URL.
      {
        path: 'upload',
        canActivate: [tokenGuard],
        loadComponent: () => import('./app/pages/page.upload').then((m) => m.Upload),
      },
      {
        path: 'summary',
        canActivate: [tokenGuard],
        loadComponent: () => import('./app/pages/page.summary').then((m) => m.Summary),
      },
      {
        path: 'download',
        canActivate: [tokenGuard],
        loadComponent: () => import('./app/pages/page.download').then((m) => m.Download),
      },
      // Authentication + account pages.
      { path: 'login', loadComponent: () => import('./app/pages/page.login').then((m) => m.Login) },
      {
        path: 'login/verify',
        loadComponent: () => import('./app/pages/page.login-verify').then((m) => m.LoginVerify),
      },
      {
        path: 'sessions',
        canActivate: [authGuard],
        loadComponent: () => import('./app/pages/page.sessions').then((m) => m.Sessions),
      },
      {
        path: 'help',
        canActivate: [authGuard],
        loadComponent: () => import('./app/pages/page.help').then((m) => m.Help),
      },
    ],
  },
];
