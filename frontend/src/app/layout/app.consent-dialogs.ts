import { Component, inject } from '@angular/core';
import { Router } from '@angular/router';
import { ButtonModule } from 'primeng/button';
import { DialogModule } from 'primeng/dialog';

import { PageService } from '../pages/page.service';

@Component({
  selector: 'app-consent-dialogs',
  imports: [ButtonModule, DialogModule],
  template: `
    <p-dialog
      header="Consent Required"
      [visible]="pageService.consentRequired()"
      [modal]="true"
      [closable]="false"
      [dismissableMask]="false"
      [style]="{ width: '30rem' }"
    >
      <p class="text-base leading-relaxed">
        To continue using the Service, you must agree to the "Terms of Service" and "Privacy Policy"
        by checking the checkbox on the "Instructions" page.
      </p>
      <ng-template pTemplate="footer">
        <p-button label="OK" (click)="onConsentDialogOk()" />
      </ng-template>
    </p-dialog>

    <p-dialog
      header="Session Expired"
      [visible]="pageService.pageState().expiredSession"
      [modal]="true"
      [closable]="false"
      [dismissableMask]="false"
      [style]="{ width: '30rem' }"
    >
      <p class="text-base leading-relaxed">
        Your session has expired. Please start a new session from the Instructions page.
      </p>
      <ng-template pTemplate="footer">
        <p-button label="OK" (click)="onExpiredDialogOk()" />
      </ng-template>
    </p-dialog>
  `,
})
export class AppConsentDialogs {
  pageService = inject(PageService);
  private router = inject(Router);

  onConsentDialogOk(): void {
    this.pageService.consentRequired.set(false);
    const token = this.pageService.pageState().tokenBase;
    this.router.navigate(['/info'], token ? { queryParams: { token } } : {});
  }

  onExpiredDialogOk(): void {
    this.pageService.tokenValidation.set(null);
    this.pageService.pageState.update((prev) => ({
      ...prev,
      expiredSession: false,
      tokenBase: null,
      conversionId: null,
      consentedTo: false,
      firstConsent: true,
    }));
    this.router.navigate(['/info']);
  }
}
