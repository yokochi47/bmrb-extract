import { Component } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterLink } from '@angular/router';

import { ONEDEP_URL, BMRBDEP_URL } from '../../site.config';
import { TermsConsent } from './terms.consent';

@Component({
  selector: 'app-info',
  imports: [CommonModule, RouterLink, TermsConsent],
  templateUrl: './info.html',
})
export class Info {
  ONEDEP_URL = ONEDEP_URL;
  BMRBDEP_URL = BMRBDEP_URL;

  subscribeCesentChanged(agreed: boolean) {
    console.log('Agreed', agreed);
  }
}
