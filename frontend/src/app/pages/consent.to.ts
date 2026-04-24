import { Component, inject } from '@angular/core';
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

  state = this.pageService.pageState();

  consentedTo = this.state.consentedTo;
  locked = this.state.lockedSession || this.state.expiredSession || !this.state.firstUpload;

  onChange() {
    this.pageService.pageState.update((prev) => ({
      ...prev,
      consentedTo: this.consentedTo,
    }));
    console.log('consentedTo', this.consentedTo);
  }
}
