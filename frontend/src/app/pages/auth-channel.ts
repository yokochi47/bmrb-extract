import { Injectable } from '@angular/core';
import { Observable, Subject } from 'rxjs';

/** Cross-tab auth notifications, exchanged over a same-origin BroadcastChannel.
 *
 * The magic link is opened by the mail application in a *new* tab, so the tab
 * that requested it would otherwise never learn that login succeeded — even
 * though the session cookie (path=/, host-only) is already valid for the whole
 * origin at that point. `login` announces that; a tab still waiting for the link
 * answers with `login-ack` so the opening tab knows it can hand over. */
export type AuthBroadcast =
  | { type: 'login'; totp_required: boolean }
  | { type: 'login-ack' }
  | { type: 'logout' };

const CHANNEL_NAME = 'bmrbx_auth';

function isAuthBroadcast(m: unknown): m is AuthBroadcast {
  const t = (m as AuthBroadcast | null)?.type;
  return t === 'login' || t === 'login-ack' || t === 'logout';
}

/**
 * Thin BroadcastChannel wrapper. Messages are delivered to the *other* browsing
 * contexts of the origin only, never back to the sender, so no echo handling is
 * needed. Where BroadcastChannel is unavailable this degrades to a no-op: `post`
 * does nothing and `messages` never emits, leaving every caller on its
 * single-tab code path.
 */
@Injectable({ providedIn: 'root' })
export class AuthChannel {
  private channel: BroadcastChannel | null = null;
  private subject = new Subject<AuthBroadcast>();

  /** Auth events from other tabs of this browser. */
  readonly messages: Observable<AuthBroadcast> = this.subject.asObservable();

  constructor() {
    try {
      if (typeof BroadcastChannel !== 'undefined') {
        this.channel = new BroadcastChannel(CHANNEL_NAME);
        this.channel.onmessage = (ev: MessageEvent) => {
          if (isAuthBroadcast(ev.data)) this.subject.next(ev.data);
        };
      }
    } catch {
      // Unsupported or blocked (some privacy modes) — stay silent.
      this.channel = null;
    }
  }

  post(message: AuthBroadcast): void {
    try {
      this.channel?.postMessage(message);
    } catch {
      // A failed hand-off is never fatal; the other tab simply stays as it was.
    }
  }
}
