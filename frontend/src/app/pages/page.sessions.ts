import { Component, inject, signal } from '@angular/core';
import { TitleCasePipe } from '@angular/common';
import { Router } from '@angular/router';
import { TableModule } from 'primeng/table';
import { ButtonModule } from 'primeng/button';
import { MessageModule } from 'primeng/message';
import { TagModule } from 'primeng/tag';

import { AuthService, SessionRow } from './auth.service';

/** Lists the signed-in user's sessions; annotators can toggle to every session
 * (opened via the audited token_admin). Each row reopens the session by putting
 * its token in the URL, which the existing pages restore. */
@Component({
  selector: 'app-sessions',
  imports: [TitleCasePipe, TableModule, ButtonModule, MessageModule, TagModule],
  templateUrl: './page.sessions.html',
})
export class Sessions {
  private auth = inject(AuthService);
  private router = inject(Router);

  isAdmin = this.auth.isAdmin;
  /** Conversion IDs needing attention (user: new reply; admin: awaiting reply). */
  unreadIds = this.auth.unreadIds;
  rows = signal<SessionRow[]>([]);
  scope = signal<string>('own');
  loading = signal(true);
  error = signal<string | null>(null);

  constructor() {
    this.load(false);
    this.auth.refreshUnread();
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

  /** Human-readable label for the target deposition system + workflow. */
  depsysLabel(code: string): string {
    return (
      {
        onedep: 'OneDep (new deposition)',
        repl_cs: 'OneDep (ongoing deposition)',
        bmrbdep: 'BMRBdep (new deposition)',
      }[code] ?? code
    );
  }

  /** Token to reopen a row: the user's own token, or (admin) the token_admin. */
  openToken(row: SessionRow): string {
    return row.token ?? row.token_admin ?? '';
  }

  /** Reopen a session by putting its token in the URL (existing pages restore it). */
  open(row: SessionRow): void {
    const token = this.openToken(row);
    if (token) this.router.navigate(['/info'], { queryParams: { token } });
  }

  shortDate(iso: string | null): string {
    return iso ? iso.slice(0, 10) : '';
  }
}
