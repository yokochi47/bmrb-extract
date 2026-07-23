import { Component, computed, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Router } from '@angular/router';
import { TagModule } from 'primeng/tag';
import { OverlayBadgeModule } from 'primeng/overlaybadge';

import { LayoutService } from './layout.service';
import { PageService } from '../pages/page.service';
import { AuthService } from '../pages/auth.service';

@Component({
  selector: 'app-topbar',
  imports: [CommonModule, TagModule, OverlayBadgeModule],
  templateUrl: 'app.topbar.html',
})
export class AppTopbar {
  layoutService = inject(LayoutService);
  pageService = inject(PageService);
  private auth = inject(AuthService);
  private router = inject(Router);

  authenticated = this.auth.authenticated;
  /** Unread help-desk items (annotator: awaiting reply; user: new replies). */
  unreadCount = this.auth.unreadCount;

  conversionLabel = computed(() => {
    const id = this.pageService.pageState().conversionId;
    return id !== null ? `C_${id}` : 'Not yet issued';
  });

  /** Open the help desk (where the inquiries / replies live). */
  openMessages() {
    this.router.navigate(['/help']);
  }
}
