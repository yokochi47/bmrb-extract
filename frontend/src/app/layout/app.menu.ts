import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterModule } from '@angular/router';
import { MenuItem } from 'primeng/api';

import { AppMenuitem } from './app.menuitem';

@Component({
  selector: 'app-menu',
  standalone: true,
  imports: [CommonModule, RouterModule, AppMenuitem],
  templateUrl: './app.menu.html',
})
export class AppMenu implements OnInit {
  model: MenuItem[] = [];

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
          },
          {
            label: 'Upload files',
            icon: 'pi pi-fw pi-upload',
            routerLink: ['/upload'],
          },
          {
            label: 'Upload summary',
            icon: 'pi pi-fw pi-check-square',
            routerLink: ['/summary'],
          },
          {
            label: 'Download',
            icon: 'pi pi-fw pi-download',
            routerLink: ['/download'],
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
          },
          {
            label: 'Terms and conditions',
            icon: 'pi pi-fw pi-bookmark',
            routerLink: ['/terms'],
          },
          {
            label: 'Privacy policy',
            icon: 'pi pi-fw pi-bookmark',
            routerLink: ['/privacy'],
          },
        ],
      },
    ];
  }
}
