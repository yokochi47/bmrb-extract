import { Component, computed, inject } from '@angular/core';
import { RouterLink } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { CheckboxModule } from 'primeng/checkbox';

import { PageService } from './page.service';

@Component({
  selector: 'app-consent-to',
  imports: [RouterLink, FormsModule, CheckboxModule],
  templateUrl: './consent.to.html',
})
export class ConsentTo {
  pageService = inject(PageService);

  consentedTo = computed(() => this.pageService.pageState().consentedTo);
  locked = computed(() => {
    const s = this.pageService.pageState();
    return s.lockedSession || s.expiredSession || !s.firstUpload;
  });

  onChange() {
    const s = this.pageService.pageState();
    this.pageService.pageState.update((prev) => ({
      ...prev,
      consentedTo: !s.consentedTo,
    }));
  }
}
