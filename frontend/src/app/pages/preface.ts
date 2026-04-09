import { Component } from '@angular/core';
import { DividerModule } from 'primeng/divider';

@Component({
  selector: 'app-preface',
  standalone: true,
  imports: [DividerModule],
  templateUrl: './preface.html',
})
export class Preface {}
