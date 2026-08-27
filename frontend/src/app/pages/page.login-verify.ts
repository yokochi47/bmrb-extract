import { Component, inject, signal } from '@angular/core';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { MessageModule } from 'primeng/message';

import { AuthService } from './auth.service';

/** How long to wait for the tab the user started from to claim the login before
 * concluding there is none and continuing here. */
const HANDOFF_WAIT_MS = 500;

/** Landing page for the emailed magic link (/login/verify?c=…): consumes the
 * token, then hands back to the tab the user started from — or, when there is
 * none, routes to the TOTP step (annotators) or the sessions list. */
@Component({
  selector: 'app-login-verify',
  imports: [MessageModule, RouterLink],
  templateUrl: './page.login-verify.html',
})
export class LoginVerify {
  private auth = inject(AuthService);
  private route = inject(ActivatedRoute);
  private router = inject(Router);

  status = signal<'checking' | 'error' | 'handed-off'>('checking');
  /** Handed off to a tab that still owes the annotator second factor. */
  totpPending = signal(false);

  constructor() {
    const c = this.route.snapshot.queryParamMap.get('c');
    if (!c) {
      this.status.set('error');
      return;
    }
    this.auth.verify(c).subscribe({
      next: (s) => {
        // verify() announced the login to the other tabs; the session cookie is
        // valid origin-wide, so a waiting tab can simply continue on its own.
        this.auth.awaitLoginAck(HANDOFF_WAIT_MS).subscribe((acked) => {
          if (!acked) {
            // Annotators still need the second factor before admin authority.
            this.router.navigate([s.totp_required ? '/login' : '/sessions']);
            return;
          }
          this.totpPending.set(!!s.totp_required);
          this.status.set('handed-off');
          // Only works for script-opened windows; the message is the real answer.
          try {
            window.close();
          } catch {
            // ignore
          }
        });
      },
      error: () => this.status.set('error'),
    });
  }
}
