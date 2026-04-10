import { Component, EventEmitter, Output } from '@angular/core';
import { RouterLink } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { CheckboxModule } from 'primeng/checkbox';

@Component({
  selector: 'app-terms-consent',
  imports: [RouterLink, FormsModule, CheckboxModule],
  templateUrl: './terms.consent.html',
  outputs: ['consentChanged'],
})
export class TermsConsent {
  agreed = false;

  @Output() consentChanged = new EventEmitter<boolean>();

  onChange() {
    this.consentChanged.emit(this.agreed);
  }
}
