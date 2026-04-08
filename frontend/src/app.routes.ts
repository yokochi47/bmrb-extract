import { Routes } from '@angular/router';

import { AppLayout } from './app/layout/app.layout';
import { Info } from './app/pages/info';
import { Terms } from './app/pages/terms';
import { Privacy } from './app/pages/privacy';

export const appRoutes: Routes = [
  {
    path: '',
    component: AppLayout,
    children: [
      { path: '', redirectTo: 'info', pathMatch: 'full' },
      { path: 'info', component: Info },
      { path: 'terms', component: Terms },
      { path: 'privacy', component: Privacy },
    ],
  },
];
