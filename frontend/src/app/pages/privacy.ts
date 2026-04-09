import { Component } from '@angular/core';
import { DividerModule } from 'primeng/divider';

import {
  SERVICE_HELP_EMAIL,
  SUCCESS_VALIDITY_PERIOD_IN_DAYS,
  FAILURE_VALIDITY_PERIOD_IN_DAYS,
} from '../../site.config';

@Component({
  selector: 'app-privacy',
  imports: [DividerModule],
  templateUrl: './privacy.html',
})
export class Privacy {
  SERVICE_HELP_EMAIL = SERVICE_HELP_EMAIL;
  SUCCESS_VALIDITY_PERIOD_IN_DAYS = SUCCESS_VALIDITY_PERIOD_IN_DAYS;
  FAILURE_VALIDITY_PERIOD_IN_DAYS = FAILURE_VALIDITY_PERIOD_IN_DAYS;
}
