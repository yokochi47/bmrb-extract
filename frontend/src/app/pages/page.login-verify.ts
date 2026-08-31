import { Component, inject, signal } from '@angular/core';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { ButtonModule } from 'primeng/button';
import { MessageModule } from 'primeng/message';

import { AuthService } from './auth.service';

/** How long to wait for the tab the user started from to claim the login before
 * concluding there is none and continuing here. */
const HANDOFF_WAIT_MS = 500;

/** Landing page for the emailed sign-in link (/login/verify?c=…&p=…).
 *
 * The link is often opened on a different device from the one the user started on
 * — mail lives on a phone, the files live on a workstation — and the session
 * cookie can only ever be set on the device that makes the request. So this page
 * first decides which device it is on:
 *
 * - **The browser that asked for the link** (it holds the pending-login stash for
 *   this very handle): consume the token as before, then hand back to the waiting
 *   tab over the BroadcastChannel, or continue here if there is none.
 * - **Any other browser**: do *not* consume the token. Point the user back to the
 *   device they started on, where the emailed code is waiting to be typed. An
 *   escape hatch still lets them sign in here — except for annotators, whom the
 *   backend refuses, since their second factor belongs on that other device.
 */
@Component({
  selector: 'app-login-verify',
  imports: [ButtonModule, MessageModule, RouterLink],
  templateUrl: './page.login-verify.html',
})
export class LoginVerify {
  private auth = inject(AuthService);
  private route = inject(ActivatedRoute);
  private router = inject(Router);

  status = signal<'checking' | 'error' | 'handed-off' | 'other-device' | 'annot-blocked'>(
    'checking',
  );
  /** Handed off to a tab that still owes the annotator second factor. */
  totpPending = signal(false);

  private c: string | null = null;

  constructor() {
    this.c = this.route.snapshot.queryParamMap.get('c');
    const p = this.route.snapshot.queryParamMap.get('p');
    if (!this.c) {
      this.status.set('error');
      return;
    }
    const stash = this.auth.pendingLogin();
    // Same browser = this profile holds the stash for *this* challenge. Matching
    // the handle, rather than merely noting that some stash exists, rules out a
    // stale one left by a login the user once abandoned on this device. A missing
    // ?p= means a mail sent before this feature existed — fall back to the old
    // behaviour so links already in flight keep working.
    const sameBrowser = !!stash && (p === null || stash.pending_id === p);
    if (sameBrowser) {
      this.doVerify(false);
      return;
    }
    // Leave the challenge unconsumed: the code is still waiting to be typed on the
    // device the user is actually working at.
    this.status.set('other-device');
  }

  /** Escape hatch: this really is the device the user is working on (a different
   * browser, a private window, cleared storage). Annotators are refused by the
   * backend, which leaves the link unused. */
  verifyHere(): void {
    this.status.set('checking');
    this.doVerify(true);
  }

  private doVerify(crossDevice: boolean): void {
    this.auth.verify(this.c as string, crossDevice).subscribe({
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
      error: (e: { status?: number; error?: { error?: string } }) => {
        this.status.set(
          e?.status === 403 && e?.error?.error === 'annotator_must_use_code'
            ? 'annot-blocked'
            : 'error',
        );
      },
    });
  }
}
