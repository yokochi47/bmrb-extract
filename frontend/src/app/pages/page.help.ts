import { Component, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ButtonModule } from 'primeng/button';
import { CardModule } from 'primeng/card';
import { InputTextModule } from 'primeng/inputtext';
import { TextareaModule } from 'primeng/textarea';
import { SelectModule } from 'primeng/select';
import { MessageModule } from 'primeng/message';
import { DividerModule } from 'primeng/divider';
import { TagModule } from 'primeng/tag';

import { AuthService, Inquiry, SessionRow } from './auth.service';

interface Thread {
  conversion_id: number;
  public_id: string;
  messages: Inquiry[];
}

/** Help desk (Terms #5): signed-in users file inquiries about one of their
 * processed sessions; annotators read every thread and reply. */
@Component({
  selector: 'app-help',
  imports: [
    FormsModule,
    ButtonModule,
    CardModule,
    InputTextModule,
    TextareaModule,
    SelectModule,
    MessageModule,
    DividerModule,
    TagModule,
  ],
  templateUrl: './page.help.html',
})
export class Help {
  private auth = inject(AuthService);

  isAdmin = this.auth.isAdmin;
  /** Conversion IDs needing attention (user: new reply; admin: awaiting reply). */
  unreadIds = this.auth.unreadIds;
  loading = signal(true);

  // --- user side ---
  ownSessions = signal<SessionRow[]>([]);
  /** Options for the p-select session picker (composite label + token value).
   * Sessions with a new annotator reply are flagged in the label. */
  sessionOptions = computed(() => {
    const unread = this.unreadIds();
    return this.ownSessions().map((s) => ({
      label:
        `${s.public_id} (${this.titleCase(s.status)})` +
        (s.conversion_id != null && unread.has(s.conversion_id) ? ' — new reply' : ''),
      value: s.token,
    }));
  });
  selectedToken = signal('');
  subject = signal('');
  content = signal('');
  userBusy = signal(false);
  userSent = signal(false);
  userError = signal<string | null>(null);
  /** Message thread for the selected session (newest first). */
  thread = signal<Inquiry[]>([]);
  threadLoading = signal(false);

  // --- annotator side ---
  inquiries = signal<Inquiry[]>([]);
  replies = signal<Record<number, string>>({});
  threads = computed<Thread[]>(() => {
    const byId = new Map<number, Thread>();
    for (const m of this.inquiries()) {
      let t = byId.get(m.conversion_id);
      if (!t) {
        t = { conversion_id: m.conversion_id, public_id: m.public_id, messages: [] };
        byId.set(m.conversion_id, t);
      }
      t.messages.push(m);
    }
    return [...byId.values()];
  });

  constructor() {
    if (this.auth.isAdmin()) {
      this.loadInquiries();
    } else {
      this.auth.listSessions().subscribe({
        next: (r) => {
          this.ownSessions.set(r.sessions.filter((s) => s.conversion_id != null && s.token));
          this.loading.set(false);
        },
        error: () => this.loading.set(false),
      });
    }
  }

  private loadInquiries() {
    this.auth.getInquiries().subscribe({
      next: (r) => {
        this.inquiries.set(r.inquiries);
        this.loading.set(false);
      },
      error: () => this.loading.set(false),
    });
  }

  /** Capitalize the first character (status values are single lowercase words). */
  private titleCase(s: string): string {
    return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
  }

  private msgTime(m: Inquiry): string {
    return m.sent_at ? m.sent_at.slice(0, 16).replace('T', ' ') : '';
  }

  /** Card subheader (user view): "You · 2026-07-23 14:05" / "Annotator · …". */
  messageMeta(m: Inquiry): string {
    const who = m.from_admin ? 'Annotator' : 'You';
    const when = this.msgTime(m);
    return when ? `${who} · ${when}` : who;
  }

  /** Card subheader (admin view): the sender's address (or "Annotator") · time. */
  adminMessageMeta(m: Inquiry): string {
    const who = m.from_admin ? 'Annotator' : m.email_address;
    const when = this.msgTime(m);
    return when ? `${who} · ${when}` : who;
  }

  /** Select a session: reset transient state and load its message thread. */
  onSelectSession(token: string) {
    this.selectedToken.set(token);
    this.userSent.set(false);
    this.userError.set(null);
    this.loadThread(token);
  }

  /** Load the selected session's thread (own inquiries + annotator replies). */
  loadThread(token: string) {
    if (!token) {
      this.thread.set([]);
      return;
    }
    this.threadLoading.set(true);
    this.auth.getThread(token).subscribe({
      next: (r) => {
        this.thread.set(r.messages);
        this.threadLoading.set(false);
        // Opening the thread marked replies seen server-side — clear the badge.
        this.auth.refreshUnread();
      },
      error: () => {
        this.thread.set([]);
        this.threadLoading.set(false);
      },
    });
  }

  submitInquiry() {
    const token = this.selectedToken();
    const subject = this.subject().trim();
    const content = this.content().trim();
    if (!token || !subject || !content || this.userBusy()) {
      this.userError.set('Please pick a session and enter a subject and message.');
      return;
    }
    this.userBusy.set(true);
    this.userError.set(null);
    this.auth.postInquiry(token, subject, content).subscribe({
      next: () => {
        this.userBusy.set(false);
        this.userSent.set(true);
        this.subject.set('');
        this.content.set('');
        // Reload so the just-sent message appears at the top of the thread.
        this.loadThread(token);
        this.auth.refreshUnread();
      },
      error: () => {
        this.userBusy.set(false);
        this.userError.set('Could not send your inquiry — please try again.');
      },
    });
  }

  setReply(cid: number, value: string) {
    this.replies.update((r) => ({ ...r, [cid]: value }));
  }

  submitReply(cid: number) {
    const content = (this.replies()[cid] || '').trim();
    if (!content) return;
    this.auth.postReply(cid, content).subscribe({
      next: () => {
        this.setReply(cid, '');
        this.loadInquiries();
        // Replying handles the inquiry on this site — clear the badge.
        this.auth.refreshUnread();
      },
    });
  }
}
