import { Routes } from '@angular/router';
import { AppLayout } from './app/layout/app.layout';
import { AppInfo } from './app/pages/app.info';

export const appRoutes: Routes = [
  {
    path: '',
    component: AppLayout,
    children: [{ path: '', component: AppInfo }],
  },
];
