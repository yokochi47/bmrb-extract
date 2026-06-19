import {
  Component,
  ElementRef,
  OnDestroy,
  computed,
  effect,
  inject,
  signal,
  viewChild,
} from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { CardModule } from 'primeng/card';
import { TableModule } from 'primeng/table';

import { PageService, TargetDepsys } from './page.service';
import { API_URL } from '../../site.config';
import { fileTypeLabel } from './file-types';
import { MolstarViewer } from './molstar';

/** A selected upload file participating in the latest conversion run. */
interface UploadFileRow {
  original_name: string;
  file_size: number;
  file_type: string;
  source: string;
  /** Upload time as a naive UTC string ("YYYY-MM-DD HH:mm"); see GET /api/files. */
  uploaded_at: string | null;
}

@Component({
  selector: 'app-summary',
  imports: [CardModule, TableModule],
  templateUrl: './page.summary.html',
})
export class Summary implements OnDestroy {
  private pageService = inject(PageService);
  private http = inject(HttpClient);

  /** Selected files of the latest run, ordered by upload time (server-side). */
  files = signal<UploadFileRow[]>([]);

  /** The Source column is shown only when at least one file did not come from
   * the user (i.e. was downloaded from BMRB). */
  showSource = computed(() => this.files().some((f) => f.source !== 'user'));

  /** The coordinate preview is shown only for targets that produce a converted
   * coordinate file (onedep / repl_cs); bmrbdep has none. */
  showViewer = computed(() => {
    const t = this.pageService.pageState().targetDepsys;
    return t === TargetDepsys.onedep || t === TargetDepsys.repl_cs;
  });

  /** Set when the Mol* preview could not be loaded (e.g. no coordinate yet). */
  viewerError = signal(false);

  /** Host element for the Mol* canvas (only present while showViewer()). */
  private coordinateHost = viewChild<ElementRef<HTMLDivElement>>('molstarHost');

  private fetched = false;
  private viewerInit = false;
  private viewer: MolstarViewer | null = null;

  /** Shared, one-shot loader for the prebuilt Mol* bundle assets. */
  private static molstarLoaded?: Promise<void>;

  constructor() {
    // Load the file list once the session token is available (covers direct
    // navigation, refresh, and arriving from the processing dialog).
    effect(() => {
      const token = this.pageService.pageState().tokenBase;
      if (token && !this.fetched) {
        this.fetched = true;
        this.loadFiles(token);
      }
    });

    // Initialise the Mol* coordinate preview once the token is known, the
    // target qualifies, and the host element has been rendered (viewChild is a
    // signal, so this effect re-runs when the @if reveals the host).
    effect(() => {
      const token = this.pageService.pageState().tokenBase;
      const host = this.coordinateHost()?.nativeElement;
      if (!token || !this.showViewer() || !host || this.viewerInit) return;
      this.viewerInit = true;
      void this.initViewer(token, host);
    });
  }

  ngOnDestroy(): void {
    this.disposeViewer();
  }

  private loadFiles(token: string): void {
    this.http
      .get<{ files: UploadFileRow[] }>(API_URL + 'files', { params: { token } })
      .subscribe({
        next: (res) => this.files.set(res.files ?? []),
        error: (err) => console.error('Failed to load upload files', err),
      });
  }

  /** Lazily inject the prebuilt Mol* bundle (served at /molstar/) once. */
  private loadMolstarAssets(): Promise<void> {
    if (Summary.molstarLoaded) return Summary.molstarLoaded;
    Summary.molstarLoaded = new Promise<void>((resolve, reject) => {
      const link = document.createElement('link');
      link.rel = 'stylesheet';
      link.href = '/molstar/molstar.css';
      document.head.appendChild(link);

      const script = document.createElement('script');
      script.src = '/molstar/molstar.js';
      script.onload = () => resolve();
      script.onerror = () => reject(new Error('Failed to load Mol*'));
      document.head.appendChild(script);
    });
    return Summary.molstarLoaded;
  }

  /** Create the Mol* viewer and load the converted coordinate (mmCIF). */
  private async initViewer(token: string, host: HTMLElement): Promise<void> {
    try {
      await this.loadMolstarAssets();
      if (!window.molstar) throw new Error('Mol* global unavailable');
      const viewer = await window.molstar.Viewer.create(host, {
        layoutIsExpanded: false,
        layoutShowControls: false,
        layoutShowRemoteState: false,
        layoutShowSequence: false,
        layoutShowLog: false,
        layoutShowLeftPanel: false,
        viewportShowExpand: true, // built-in fullscreen toggle
        viewportShowSelectionMode: false,
        viewportShowAnimation: false,
      });
      this.viewer = viewer;
      const url = `${API_URL}coordinate?token=${encodeURIComponent(token)}`;
      // Rejects on a 404 (no coordinate) → caught below to show the fallback.
      await viewer.loadStructureFromUrl(url, 'mmcif', false);
    } catch (err) {
      console.error('Mol* coordinate preview unavailable', err);
      this.viewerError.set(true);
      this.disposeViewer();
    }
  }

  /** Dispose the Mol* viewer (frees its WebGL context + workers). */
  private disposeViewer(): void {
    try {
      this.viewer?.dispose();
    } catch {
      /* already torn down */
    }
    this.viewer = null;
  }

  /** Human-readable file-type label. */
  typeLabel(value: string): string {
    return fileTypeLabel(value);
  }

  sourceLabel(source: string): string {
    return source === 'bmrb' ? 'BMRB' : 'User';
  }

  formatSize(bytes: number): string {
    if (!bytes) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(1024));
    return `${(bytes / Math.pow(1024, i)).toFixed(1)} ${units[i]}`;
  }
}
