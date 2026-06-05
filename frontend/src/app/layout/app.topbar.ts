import { Component, computed, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { TagModule } from 'primeng/tag';

import { LayoutService } from './layout.service';
import { PageService } from '../pages/page.service';

@Component({
  selector: 'app-topbar',
  imports: [CommonModule, TagModule],
  templateUrl: 'app.topbar.html',
})
export class AppTopbar {
  layoutService = inject(LayoutService);
  pageService = inject(PageService);

  conversionLabel = computed(() => {
    const id = this.pageService.pageState().conversionId;
    return id !== null ? `C_${id}` : 'Not yet issued';
  });
}
