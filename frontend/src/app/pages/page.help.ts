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

import { AuthService, Inquiry, SessionRow, ThreadState } from './auth.service';
import { PageService } from './page.service';

interface Thread {
  conversion_id: number;
  public_id: string;
  messages: Inquiry[];
  /** The newest message is an annotator reply (derived from `messages`, so it
   * flips as soon as a reply is reloaded -- no wait for the unread poll). */
  answered: boolean;
  /** An annotator marked this thread resolved and no newer inquiry reopened it. */
  resolved: boolean;
  /** Handled either way, so it belongs in the 'Addressed' group. */
  addressed: boolean;
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
  private pageService = inject(PageService);

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
  /** Per-thread handling state from the server, keyed by conversion_id. */
  states = signal<Record<number, ThreadState>>({});
  replies = signal<Record<number, string>>({});
  /** Which group the two switch buttons are showing. */
  group = signal<'open' | 'addressed'>('open');
  resolving = signal<number | null>(null);
  threads = computed<Thread[]>(() => {
    const states = this.states();
    const byId = new Map<number, Thread>();
    for (const m of this.inquiries()) {
      let t = byId.get(m.conversion_id);
      if (!t) {
        t = {
          conversion_id: m.conversion_id,
          public_id: m.public_id,
          messages: [],
          answered: false,
          resolved: !!states[m.conversion_id]?.resolved,
          addressed: false,
        };
        byId.set(m.conversion_id, t);
      }
      t.messages.push(m);
    }
    // Messages arrive ordinal-ascending, so the last one is the newest.
    for (const t of byId.values()) {
      t.answered = !!t.messages[t.messages.length - 1]?.from_admin;
      t.addressed = t.answered || t.resolved;
    }
    return [...byId.values()];
  });
  /** Inquiries nobody has replied to or closed -- the annotator's work queue. */
  openThreads = computed(() => this.threads().filter((t) => !t.addressed));
  /** Replied to, or explicitly marked resolved by an annotator. */
  addressedThreads = computed(() => this.threads().filter((t) => t.addressed));
  visibleThreads = computed(() =>
    this.group() === 'open' ? this.openThreads() : this.addressedThreads(),
  );

  constructor() {
    if (this.auth.isAdmin()) {
      this.loadInquiries();
    } else {
      this.auth.listSessions().subscribe({
        next: (r) => {
          this.ownSessions.set(r.sessions.filter((s) => s.conversion_id != null && s.token));
          this.loading.set(false);
          // Coming from a session page: preselect that session (the user can
          // still pick another). Ineligible sessions are simply not listed.
          const current = this.pageService.pageState().tokenBase;
          if (
            current &&
            !this.selectedToken() &&
            this.ownSessions().some((s) => s.token === current)
          ) {
            this.onSelectSession(current);
          }
        },
        error: () => this.loading.set(false),
      });
    }
  }

  private loadInquiries() {
    this.auth.getInquiries().subscribe({
      next: (r) => {
        this.inquiries.set(r.inquiries);
        this.states.set(Object.fromEntries((r.threads ?? []).map((t) => [t.conversion_id, t])));
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

  /** Mark a thread resolved without replying, or reopen a closed one. Resolving
   * takes it out of the work queue and the badge; a newer user inquiry would
   * reopen it server-side regardless. */
  toggleResolved(cid: number, resolved: boolean) {
    if (this.resolving() !== null) return;
    this.resolving.set(cid);
    this.auth.postResolve(cid, resolved).subscribe({
      next: () => {
        this.resolving.set(null);
        this.loadInquiries();
        this.auth.refreshUnread();
      },
      error: () => this.resolving.set(null),
    });
  }
}
