import { Component } from '@angular/core';
import { HOST_SITE_URL, HOST_SITE_LOGO, ONEDEP_URL, BMRBDEP_URL } from '../../site.config';

@Component({
  selector: 'app-footer',
  templateUrl: './app.footer.html',
})
export class AppFooter {
  HOST_SITE_URL = HOST_SITE_URL;
  HOST_SITE_LOGO = HOST_SITE_LOGO;
  ONEDEP_URL = ONEDEP_URL;
  BMRBDEP_URL = BMRBDEP_URL;
}
