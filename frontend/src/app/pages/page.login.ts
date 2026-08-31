import { Component, OnDestroy, effect, inject, signal, untracked } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { ButtonModule } from 'primeng/button';
import { InputOtpModule } from 'primeng/inputotp';
import { InputTextModule } from 'primeng/inputtext';
import { MessageModule } from 'primeng/message';

import { AuthService } from './auth.service';

@Component({
  selector: 'app-login',
  imports: [FormsModule, ButtonModule, InputOtpModule, InputTextModule, MessageModule],
  templateUrl: './page.login.html',
})
export class Login implements OnDestroy {
  private auth = inject(AuthService);
  private router = inject(Router);

  authenticated = this.auth.authenticated;
  email = this.auth.email;
  role = this.auth.role;
  totpRequired = this.auth.totpRequired;
  totpEnrolled = this.auth.totpEnrolled;

  emailInput = signal('');
  // A stashed pending login means the mail is already out: come back to the code
  // form rather than the address form, even after a reload.
  sent = signal(!!this.auth.pendingLogin());
  busy = signal(false);

  /** The sign-in mail this browser is waiting on (drives the code form). */
  pendingLogin = this.auth.pendingLogin;
  loginCode = signal('');
  codeBusy = signal(false);
  codeError = signal<string | null>(null);

  qr = signal<string | null>(null);
  codeInput = signal('');
  totpError = signal<string | null>(null);
  totpBusy = signal(false);
  private enrollStarted = false;

  constructor() {
    // Returning to this page with the mail still outstanding: this is again the
    // tab waiting for the link (ngOnDestroy cleared the flag on the way out).
    if (this.auth.pendingLogin()) this.auth.awaitingMagicLink.set(true);

    // Auto-start enrollment so a first-time annotator sees the QR code immediately
    // at the TOTP step, without having to click "Begin authenticator setup". Runs
    // once (guarded), only while unenrolled; the button remains as a manual retry.
    effect(() => {
      if (this.totpRequired() && !this.totpEnrolled() && !this.qr() && !this.enrollStarted) {
        this.enrollStarted = true;
        this.startEnroll();
      }
    });

    // The emailed link is opened by the mail application in another tab; that tab
    // announces the login and AuthService has already re-read the state here. Carry
    // on where the user started, so they only have to switch back to this tab.
    // Annotators stay put: the template now renders the TOTP step instead.
    effect(() => {
      if (this.auth.loginElsewhere() === 0 || !untracked(() => this.sent())) return;
      if (untracked(() => this.totpRequired())) return;
      untracked(() => this.router.navigate(['/sessions']));
    });
  }

  ngOnDestroy(): void {
    this.auth.awaitingMagicLink.set(false);
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
        // This is now the tab waiting for the link.
        this.auth.awaitingMagicLink.set(true);
      },
      error: () => {
        this.busy.set(false);
        this.sent.set(true);
        this.auth.awaitingMagicLink.set(true);
      },
    });
  }

  /** Complete the login here, with the code from the email. This is the path for
   * a user whose mail lives on another device: the cookie is minted on this
   * response, so the session — and an annotator's TOTP step — lands on this
   * machine, the one holding their files. */
  submitCode() {
    const p = this.auth.pendingLogin();
    const c = String(this.loginCode() ?? '').replace(/\D/g, '');
    if (!p || c.length !== 6 || this.codeBusy()) return;
    this.codeBusy.set(true);
    this.codeError.set(null);
    this.auth.verifyCode(p.pending_id, c).subscribe({
      next: (s) => {
        this.codeBusy.set(false);
        this.loginCode.set('');
        // Annotators stay put — the template swaps in the TOTP step, here.
        if (!s.totp_required) this.router.navigate(['/sessions']);
      },
      error: () => {
        this.codeBusy.set(false);
        this.loginCode.set('');
        this.codeError.set('That code is not valid — check the email, or resend it.');
      },
    });
  }

  /** Send a fresh mail. The backend supersedes the previous challenge, so the old
   * link and code stop working. */
  resend() {
    const p = this.auth.pendingLogin();
    if (!p || this.busy()) return;
    this.emailInput.set(p.email);
    this.loginCode.set('');
    this.codeError.set(null);
    this.submit();
  }

  /** Abandon this login and go back to the address form. */
  useAnotherAddress() {
    this.auth.clearPendingLogin();
    this.auth.awaitingMagicLink.set(false);
    this.sent.set(false);
    this.loginCode.set('');
    this.codeError.set(null);
  }

  /** For the user who signed in on the device that opened the mail after all:
   * re-read /me rather than making this tab poll for it. */
  refreshState() {
    this.auth.refresh().subscribe({ error: () => undefined });
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
