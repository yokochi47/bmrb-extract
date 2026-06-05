import { Component, OnInit, inject } from '@angular/core';
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
export class AppMenu implements OnInit {
  model: MenuItem[] = [];

  pageService = inject(PageService);

  ngOnInit() {
    this.model = [
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
          },
          {
            label: 'Upload summary',
            icon: 'pi pi-fw pi-check-square',
            routerLink: ['/summary'],
            queryParamsHandling: 'preserve',
          },
          {
            label: 'Download',
            icon: 'pi pi-fw pi-download',
            routerLink: ['/download'],
            queryParamsHandling: 'preserve',
          },
        ],
      },
      {
        label: 'Communication',
        path: '',
        items: [
          {
            label: 'Help desk',
            icon: 'pi pi-fw pi-question-circle',
            routerLink: ['/help'],
            queryParamsHandling: 'preserve',
          },
          {
            label: 'Login',
            icon: 'pi pi-fw pi-sign-in',
            routerLink: ['/login'],
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
  }
}
