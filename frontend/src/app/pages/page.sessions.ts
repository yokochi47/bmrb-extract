import { Component, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { TableModule } from 'primeng/table';
import { ButtonModule } from 'primeng/button';
import { MessageModule } from 'primeng/message';

import { AuthService, SessionRow } from './auth.service';

/** Lists the signed-in user's sessions; annotators can toggle to every session
 * (opened via the audited token_admin). Each row reopens the session by putting
 * its token in the URL, which the existing pages restore. */
@Component({
  selector: 'app-sessions',
  imports: [RouterLink, TableModule, ButtonModule, MessageModule],
  templateUrl: './page.sessions.html',
})
export class Sessions {
  private auth = inject(AuthService);

  isAdmin = this.auth.isAdmin;
  rows = signal<SessionRow[]>([]);
  scope = signal<string>('own');
  loading = signal(true);
  error = signal<string | null>(null);

  constructor() {
    this.load(false);
  }

  load(all: boolean) {
    this.loading.set(true);
    this.error.set(null);
    this.auth.listSessions(all ? 'all' : undefined).subscribe({
      next: (r) => {
        this.rows.set(r.sessions);
        this.scope.set(r.scope);
        this.loading.set(false);
      },
      error: () => {
        this.error.set('Could not load sessions.');
        this.loading.set(false);
      },
    });
  }

  /** Token to reopen a row: the user's own token, or (admin) the token_admin. */
  openToken(row: SessionRow): string {
    return row.token ?? row.token_admin ?? '';
  }

  shortDate(iso: string | null): string {
    return iso ? iso.slice(0, 10) : '';
  }
}
