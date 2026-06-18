import { Component } from '@angular/core';
import { CardModule } from 'primeng/card';

// Placeholder "Upload summary" page — the processing dialog navigates here on
// success. Full per-run summary content is a later task.
@Component({
  selector: 'app-summary',
  imports: [CardModule],
  templateUrl: './page.summary.html',
})
export class Summary {}
