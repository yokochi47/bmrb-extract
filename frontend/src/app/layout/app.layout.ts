import { Component, computed, effect, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterModule } from '@angular/router';

import { AppTopbar } from './app.topbar';
import { AppSidebar } from './app.sidebar';
import { AppFooter } from './app.footer';
import { AppConsentDialogs } from './app.consent-dialogs';
import { LayoutService } from './layout.service';
import { PageService } from '../pages/page.service';

@Component({
  selector: 'app-layout',
  imports: [CommonModule, RouterModule, AppTopbar, AppSidebar, AppFooter, AppConsentDialogs],
  templateUrl: 'app.layout.html',
})
export class AppLayout {
  layoutService = inject(LayoutService);
  pageService = inject(PageService);

  constructor() {
    effect(() => {
      const state = this.layoutService.layoutState();
      if (state.mobileMenuActive) {
        document.body.classList.add('blocked-scroll');
      } else {
        document.body.classList.remove('blocked-scroll');
      }
    });
  }

  containerClass = computed(() => {
    const config = this.layoutService.layoutConfig();
    const state = this.layoutService.layoutState();
    return {
      'layout-overlay': config.menuMode === 'overlay',
      'layout-static': config.menuMode === 'static',
      'layout-static-inactive': state.staticMenuDesktopInactive && config.menuMode === 'static',
      'layout-overlay-active': state.overlayMenuActive,
      'layout-mobile-active': state.mobileMenuActive,
    };
  });
}
