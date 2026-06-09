import { inject } from '@angular/core';
import { CanActivateFn, Routes } from '@angular/router';
import { HttpClient } from '@angular/common/http';
import { catchError, map, of } from 'rxjs';

import { AppLayout } from './app/layout/app.layout';
import { PageService } from './app/pages/page.service';
import { API_URL } from './site.config';

/**
 * Block navigation when the session token is absent, consent is revoked,
 * or the token is no longer valid in the DB. Shows a dialog in AppLayout
 * instead of silently redirecting.
 */
export const tokenGuard: CanActivateFn = (route) => {
  const pageService = inject(PageService);
  const token = route.queryParamMap.get('token');

  if (!token || !pageService.pageState().consentedTo) {
    pageService.consentRequired.set(true);
    return false;
  }

  // Fast path: reuse cached validation result from a prior HTTP call.
  const cached = pageService.tokenValidation();
  switch (cached) {
    case 'valid':
      return true;
    case 'expired':
      pageService.pageState.update((prev) => ({ ...prev, expiredSession: true }));
      return false;
    case 'invalid':
      pageService.consentRequired.set(true);
      return false;
  }

  // Slow path: validate against DB (tokenValidation is null — first navigation).
  return inject(HttpClient)
    .get<{ conversion_id: number | null; expired: boolean }>(API_URL + 'session', {
      params: { token },
    })
    .pipe(
      map(({ conversion_id, expired }) => {
        if (expired) {
          pageService.tokenValidation.set('expired');
          pageService.pageState.update((prev) => ({ ...prev, expiredSession: true }));
          return false;
        }
        pageService.tokenValidation.set('valid');
        pageService.pageState.update((prev) => ({ ...prev, conversionId: conversion_id }));
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
      // { path: 'summary',  canActivate: [tokenGuard], loadComponent: ... },
      // { path: 'download', canActivate: [tokenGuard], loadComponent: ... },
      // { path: 'help',     canActivate: [tokenGuard], loadComponent: ... },
    ],
  },
];
