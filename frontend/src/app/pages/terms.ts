import { Component } from '@angular/core';
import { RouterLink } from '@angular/router';
import { DividerModule } from 'primeng/divider';

import {
  ONEDEP_URL,
  BMRBDEP_URL,
  SERVICE_HELP_EMAIL,
  SUCCESS_VALIDITY_PERIOD_IN_DAYS,
  FAILURE_VALIDITY_PERIOD_IN_DAYS,
} from '../../site.config';

@Component({
  selector: 'app-terms',
  imports: [RouterLink, DividerModule],
  templateUrl: './terms.html',
})
export class Terms {
  ONEDEP_URL = ONEDEP_URL;
  BMRBDEP_URL = BMRBDEP_URL;
  SERVICE_HELP_EMAIL = SERVICE_HELP_EMAIL;
  SUCCESS_VALIDITY_PERIOD_IN_DAYS = SUCCESS_VALIDITY_PERIOD_IN_DAYS;
  FAILURE_VALIDITY_PERIOD_IN_DAYS = FAILURE_VALIDITY_PERIOD_IN_DAYS;
}
