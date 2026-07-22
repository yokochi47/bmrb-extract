import { Component, inject, signal } from '@angular/core';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { MessageModule } from 'primeng/message';

import { AuthService } from './auth.service';

/** Landing page for the emailed magic link (/login/verify?c=…): consumes the
 * token, then routes to the TOTP step (annotators) or the sessions list. */
@Component({
  selector: 'app-login-verify',
  imports: [MessageModule, RouterLink],
  templateUrl: './page.login-verify.html',
})
export class LoginVerify {
  private auth = inject(AuthService);
  private route = inject(ActivatedRoute);
  private router = inject(Router);

  status = signal<'checking' | 'error'>('checking');

  constructor() {
    const c = this.route.snapshot.queryParamMap.get('c');
    if (!c) {
      this.status.set('error');
      return;
    }
    this.auth.verify(c).subscribe({
      next: (s) => {
        // Annotators still need the second factor before admin authority.
        this.router.navigate([s.totp_required ? '/login' : '/sessions']);
      },
      error: () => this.status.set('error'),
    });
  }
}
