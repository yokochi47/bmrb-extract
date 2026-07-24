import { Component } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterLink } from '@angular/router';
import { MessageModule } from 'primeng/message';

import { ONEDEP_URL, BMRBDEP_URL, SERVICE_LEVEL } from '../../site.config';
import { ConsentTo } from './consent.to';

@Component({
  selector: 'app-info',
  imports: [CommonModule, RouterLink, MessageModule, ConsentTo],
  templateUrl: './page.info.html',
})
export class Info {
  ONEDEP_URL = ONEDEP_URL;
  BMRBDEP_URL = BMRBDEP_URL;
  /** Show an "under construction" banner on non-production deployments. */
  isDevelopment = SERVICE_LEVEL === 'development';
}
