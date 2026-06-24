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

  /** Non-null when starting a session failed; prompts the user to retry. */
  consentError = this.pageService.consentError;

  locked = computed(() => {
    const state = this.pageService.pageState();
    return state.lockedSession || state.expiredSession || !state.firstUpload;
  });

  onChange() {
    const state = this.pageService.pageState();
    this.pageService.pageState.update((prev) => ({
      ...prev,
      consentedTo: !state.consentedTo,
    }));
  }
}
