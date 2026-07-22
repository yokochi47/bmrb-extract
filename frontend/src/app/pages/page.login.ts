import { Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { ButtonModule } from 'primeng/button';
import { InputTextModule } from 'primeng/inputtext';
import { MessageModule } from 'primeng/message';

import { AuthService } from './auth.service';

@Component({
  selector: 'app-login',
  imports: [FormsModule, RouterLink, ButtonModule, InputTextModule, MessageModule],
  templateUrl: './page.login.html',
})
export class Login {
  private auth = inject(AuthService);

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

  submit() {
    const e = this.emailInput().trim();
    if (!e || this.busy()) return;
    this.busy.set(true);
    // The backend responds generically (no account enumeration); we always show
    // the same "check your email" confirmation regardless of the outcome.
    this.auth.requestLogin(e).subscribe({
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
}
