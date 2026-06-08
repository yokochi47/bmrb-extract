import { inject } from '@angular/core';
import { CanActivateFn, Routes } from '@angular/router';

import { AppLayout } from './app/layout/app.layout';
import { PageService } from './app/pages/page.service';

/**
 * Block navigation and show a consent-required dialog when the session
 * token is absent from the URL. The dialog (rendered in AppLayout) handles
 * the subsequent redirect to /info.
 */
export const tokenGuard: CanActivateFn = (route) => {
  const pageService = inject(PageService);
  if (route.queryParamMap.has('token') && pageService.pageState().consentedTo) {
    return true;
  }

  pageService.consentRequired.set(true);
  return false;
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
