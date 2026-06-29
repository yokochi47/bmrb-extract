import { Component, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { PopoverModule } from 'primeng/popover';
import {
  HOST_SITE_URL,
  HOST_SITE_LOGO,
  ONEDEP_URL,
  BMRBDEP_URL,
  API_URL,
  FRONTEND_VERSION,
} from '../../site.config';

/** Live software/resource versions from the conversion images — see GET /api/versions. */
interface Versions {
  software: { maxit?: string | null; utils_nmr?: string | null };
  resource: {
    pdbx_dict?: string | null;
    ccd_co?: string | null;
    ccd_nmr?: string | null;
    cs_stat?: string | null;
  };
}

@Component({
  selector: 'app-footer',
  imports: [PopoverModule],
  templateUrl: './app.footer.html',
})
export class AppFooter {
  HOST_SITE_URL = HOST_SITE_URL;
  HOST_SITE_LOGO = HOST_SITE_LOGO;
  ONEDEP_URL = ONEDEP_URL;
  BMRBDEP_URL = BMRBDEP_URL;
  FRONTEND_VERSION = FRONTEND_VERSION;

  private http = inject(HttpClient);

  /** Image-derived versions; refreshed live by the capture-versions flow. */
  versions = signal<Versions>({ software: {}, resource: {} });

  constructor() {
    this.http.get<Versions>(API_URL + 'versions').subscribe({
      next: (v) => this.versions.set(v),
      error: (err) => console.error('Failed to load versions', err),
    });
  }
}
