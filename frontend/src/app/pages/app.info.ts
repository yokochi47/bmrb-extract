import { Component } from '@angular/core';
import { CommonModule } from '@angular/common';

import { ONEDEP_URL, BMRBDEP_URL } from '../../site.config';

@Component({
  selector: 'app-info',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './app.info.html',
})
export class AppInfo {
  ONEDEP_URL = ONEDEP_URL;
  BMRBDEP_URL = BMRBDEP_URL;
}
