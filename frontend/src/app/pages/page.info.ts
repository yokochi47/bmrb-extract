import { Component } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterLink } from '@angular/router';

import { ONEDEP_URL, BMRBDEP_URL } from '../../site.config';
import { ConsentTo } from './consent.to';

@Component({
  selector: 'app-info',
  imports: [CommonModule, RouterLink, ConsentTo],
  templateUrl: './page.info.html',
})
export class Info {
  ONEDEP_URL = ONEDEP_URL;
  BMRBDEP_URL = BMRBDEP_URL;
}
