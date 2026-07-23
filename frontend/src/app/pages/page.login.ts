import { Component, effect, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { ButtonModule } from 'primeng/button';
import { InputTextModule } from 'primeng/inputtext';
import { MessageModule } from 'primeng/message';

import { AuthService } from './auth.service';

@Component({
  selector: 'app-login',
  imports: [FormsModule, ButtonModule, InputTextModule, MessageModule],
  templateUrl: './page.login.html',
})
export class Login {
  private auth = inject(AuthService);
  private router = inject(Router);

  authenticated = this.auth.authenticated;
  email = this.auth.email;
  role = this.auth.role;
  totpRequired = this.auth.totpRequired;
  totpEnrolled = this.auth.totpEnrolled;

  emailInput = signal('');
  sent = signal(false);
  busy = signal(false);

  qr = signal<string | null>(null);
  codeInput = signal('');
  totpError = signal<string | null>(null);
  totpBusy = signal(false);
  private enrollStarted = false;

  constructor() {
    // Auto-start enrollment so a first-time annotator sees the QR code immediately
    // at the TOTP step, without having to click "Begin authenticator setup". Runs
    // once (guarded), only while unenrolled; the button remains as a manual retry.
    effect(() => {
      if (this.totpRequired() && !this.totpEnrolled() && !this.qr() && !this.enrollStarted) {
        this.enrollStarted = true;
        this.startEnroll();
      }
    });
  }

  submit() {
    const e = this.emailInput().trim();
    if (!e || this.busy()) return;
    this.busy.set(true);
    // Carry the session the user is claiming (from the URL or the stash) with the
    // login request so it is adopted on verify — even if the emailed link is
    // opened on another device.
    const claimToken = this.auth.pendingClaimToken();
    // The backend responds generically (no account enumeration); we always show
    // the same "check your email" confirmation regardless of the outcome.
    this.auth.requestLogin(e, claimToken).subscribe({
      next: () => {
        this.busy.set(false);
        this.sent.set(true);
      },
      error: () => {
        this.busy.set(false);
        this.sent.set(true);
      },
    });
  }

  startEnroll() {
    this.totpError.set(null);
    this.auth.enrollTotp().subscribe({
      next: (r) => this.qr.set(r.qr),
      error: () => this.totpError.set('Could not start authenticator setup — please retry.'),
    });
  }

  submitTotp() {
    const c = this.codeInput().trim();
    if (!c || this.totpBusy()) return;
    this.totpBusy.set(true);
    this.totpError.set(null);
    this.auth.verifyTotp(c).subscribe({
      next: () => {
        this.totpBusy.set(false);
        this.codeInput.set('');
      },
      error: () => {
        this.totpBusy.set(false);
        this.totpError.set('Invalid or expired code — please try again.');
      },
    });
  }

  logout() {
    this.auth.logout().subscribe();
  }

  /** Navigate to a protected page (used by the signed-in shortcut buttons). */
  goTo(path: string) {
    this.router.navigate([path]);
  }
}
