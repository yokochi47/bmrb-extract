import { Component, computed, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterModule } from '@angular/router';
import { MenuItem } from 'primeng/api';

import { AppMenuitem } from './app.menuitem';
import { PageService } from '../pages/page.service';

@Component({
  selector: 'app-menu',
  imports: [CommonModule, RouterModule, AppMenuitem],
  templateUrl: './app.menu.html',
})
export class AppMenu {
  pageService = inject(PageService);

  // Protected pages (Upload files / Upload summary / Download) stay disabled
  // until the user agrees to the policy, and re-disable when consent is revoked.
  model = computed<MenuItem[]>(() => {
    const consented = this.pageService.pageState().consentedTo;
    return [
      {
        label: 'Navigation',
        path: '',
        items: [
          {
            label: 'Instructions',
            icon: 'pi pi-fw pi-info-circle',
            routerLink: ['/info'],
            queryParamsHandling: 'preserve',
          },
          {
            label: 'Upload files',
            icon: 'pi pi-fw pi-upload',
            routerLink: ['/upload'],
            queryParamsHandling: 'preserve',
            disabled: !consented,
          },
          {
            label: 'Upload summary',
            icon: 'pi pi-fw pi-check-square',
            routerLink: ['/summary'],
            queryParamsHandling: 'preserve',
            disabled: !consented,
          },
          {
            label: 'Download',
            icon: 'pi pi-fw pi-download',
            routerLink: ['/download'],
            queryParamsHandling: 'preserve',
            disabled: !consented,
          },
        ],
      },
      {
        label: 'Communication',
        path: '',
        items: [
          {
            label: 'Login',
            icon: 'pi pi-fw pi-sign-in',
            routerLink: ['/login'],
          },
          {
            label: 'Help desk',
            icon: 'pi pi-fw pi-question-circle',
            routerLink: ['/help'],
            queryParamsHandling: 'preserve',
            disabled: !consented,
          },
        ],
      },
      {
        label: 'Document',
        path: '',
        items: [
          {
            label: 'Preface',
            icon: 'pi pi-fw pi-bookmark',
            routerLink: ['/preface'],
            queryParamsHandling: 'preserve',
          },
          {
            label: 'Terms and Conditions',
            icon: 'pi pi-fw pi-bookmark',
            routerLink: ['/terms'],
            queryParamsHandling: 'preserve',
          },
          {
            label: 'Privacy Policy',
            icon: 'pi pi-fw pi-bookmark',
            routerLink: ['/privacy'],
            queryParamsHandling: 'preserve',
          },
        ],
      },
    ];
  });
}
