import { Component, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { TagModule } from 'primeng/tag';

import { LayoutService } from './layout.service';

@Component({
  selector: 'app-topbar',
  imports: [CommonModule, TagModule],
  templateUrl: 'app.topbar.html',
})
export class AppTopbar {
  layoutService = inject(LayoutService);
}
