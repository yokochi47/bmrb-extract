import { Component, computed, inject } from '@angular/core';
import { RouterLink } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { CheckboxModule } from 'primeng/checkbox';

import { PageService } from './page.service';

@Component({
  selector: 'app-consent-to',
  imports: [RouterLink, FormsModule, CheckboxModule],
  templateUrl: './consent.to.html',
  // Glow the consent checkbox until it is ticked (paused for users who prefer
  // reduced motion). Matches the summary page's acknowledgment checkboxes.
  styles: [
    `
      @keyframes ack-glow {
        0%,
        100% {
          box-shadow: 0 0 0 0 rgba(245, 158, 11, 0);
        }
        50% {
          box-shadow: 0 0 8px 3px rgba(245, 158, 11, 0.75);
        }
      }
      .ack-glow {
        border-radius: 6px;
        animation: ack-glow 1.4s ease-in-out infinite;
      }
      @media (prefers-reduced-motion: reduce) {
        .ack-glow {
          animation: none;
        }
      }
    `,
  ],
})
export class ConsentTo {
  pageService = inject(PageService);

  consentedTo = computed(() => this.pageService.pageState().consentedTo);

  /** Non-null when starting a session failed; prompts the user to retry. */
  consentError = this.pageService.consentError;

  locked = computed(() => {
    const state = this.pageService.pageState();
    // Also locked once results were downloaded — the session is then read-only.
    return state.lockedSession || state.expiredSession || state.downloaded || !state.firstUpload;
  });

  onChange() {
    this.pageService.setConsent(!this.consentedTo());
  }
}
