import { inject } from '@angular/core';
import { CanActivateFn, Router, Routes } from '@angular/router';

import { AppLayout } from './app/layout/app.layout';

/** Redirect to /info when the session token is missing from the URL. */
export const tokenGuard: CanActivateFn = (route) => {
  if (route.queryParamMap.has('token')) {
    return true;
  }
  return inject(Router).createUrlTree(['/info']);
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
      // Add canActivate: [tokenGuard] when implementing these pages.
      // { path: 'upload',   canActivate: [tokenGuard], loadComponent: ... },
      // { path: 'summary',  canActivate: [tokenGuard], loadComponent: ... },
      // { path: 'download', canActivate: [tokenGuard], loadComponent: ... },
      // { path: 'help',     canActivate: [tokenGuard], loadComponent: ... },
    ],
  },
];
