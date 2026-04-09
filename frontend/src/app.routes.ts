import { Routes } from '@angular/router';

import { AppLayout } from './app/layout/app.layout';

export const appRoutes: Routes = [
  {
    path: '',
    component: AppLayout,
    children: [
      { path: '', redirectTo: 'info', pathMatch: 'full' },
      { path: 'info', loadComponent: () => import('./app/pages/info').then((m) => m.Info) },
      {
        path: 'preface',
        loadComponent: () => import('./app/pages/preface').then((m) => m.Preface),
      },
      { path: 'terms', loadComponent: () => import('./app/pages/terms').then((m) => m.Terms) },
      {
        path: 'privacy',
        loadComponent: () => import('./app/pages/privacy').then((m) => m.Privacy),
      },
    ],
  },
];
