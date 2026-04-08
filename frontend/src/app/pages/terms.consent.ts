import { Component, EventEmitter, Output } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { RadioButtonModule } from 'primeng/radiobutton';

@Component({
  selector: 'app-terms-consent',
  imports: [FormsModule, RadioButtonModule],
  templateUrl: './terms.consent.html',
})
export class TermsConsent {
  agreed = false;

  @Output() consentChanged = new EventEmitter<boolean>();

  onChange() {
    this.consentChanged.emit(this.agreed);
  }
}
