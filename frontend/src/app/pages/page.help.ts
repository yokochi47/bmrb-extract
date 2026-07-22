import { Component, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ButtonModule } from 'primeng/button';
import { InputTextModule } from 'primeng/inputtext';
import { MessageModule } from 'primeng/message';

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
  imports: [FormsModule, ButtonModule, InputTextModule, MessageModule],
  templateUrl: './page.help.html',
})
export class Help {
  private auth = inject(AuthService);

  isAdmin = this.auth.isAdmin;
  loading = signal(true);

  // --- user side ---
  ownSessions = signal<SessionRow[]>([]);
  selectedToken = signal('');
  subject = signal('');
  content = signal('');
  userBusy = signal(false);
  userSent = signal(false);
  userError = signal<string | null>(null);

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
      },
    });
  }
}
