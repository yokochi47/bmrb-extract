/**
 * Minimal typings for the prebuilt Mol* viewer bundle exposed as the global
 * `window.molstar`, loaded at runtime from `/molstar/molstar.js` (served as a
 * static asset — see angular.json and page.summary). Only the few members the
 * coordinate-preview pane uses are declared.
 */
export interface MolstarViewer {
  loadStructureFromUrl(url: string, format: string, isBinary?: boolean): Promise<void>;
  dispose(): void;
}

export interface MolstarGlobal {
  Viewer: {
    create(target: string | HTMLElement, options?: Record<string, unknown>): Promise<MolstarViewer>;
  };
}

declare global {
  interface Window {
    molstar?: MolstarGlobal;
  }
}
