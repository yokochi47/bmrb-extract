import { Injectable, effect, signal, computed } from '@angular/core';

export interface LayoutConfig {
  preset: string;
  primary: string;
  surface: string | undefined | null;
  darkTheme: boolean;
  menuMode: string;
}

interface LayoutState {
  staticMenuDesktopInactive: boolean;
  overlayMenuActive: boolean;
  mobileMenuActive: boolean;
  menuHoverActive: boolean;
  activePath: string | null;
}

@Injectable({
  providedIn: 'root',
})
export class LayoutService {
  layoutConfig = signal<LayoutConfig>({
    preset: 'Aura',
    primary: 'blue',
    surface: 'slate',
    darkTheme: false,
    menuMode: 'static',
  });

  layoutState = signal<LayoutState>({
    staticMenuDesktopInactive: false,
    overlayMenuActive: false,
    mobileMenuActive: false,
    menuHoverActive: false,
    activePath: null,
  });

  theme = computed(() => (this.layoutConfig().darkTheme ? 'light' : 'dark'));

  isSidebarActive = computed(
    () => this.layoutState().overlayMenuActive || this.layoutState().mobileMenuActive,
  );

  isDarkTheme = computed(() => this.layoutConfig().darkTheme);

  getPrimary = computed(() => this.layoutConfig().primary);

  getSurface = computed(() => this.layoutConfig().surface);

  isOverlay = computed(() => this.layoutConfig().menuMode === 'overlay');

  private initialized = false;

  constructor() {
    effect(() => {
      const config = this.layoutConfig();

      if (!this.initialized || !config) {
        this.initialized = true;
        return;
      }

      this.handleDarkModeTransition(config);
    });
  }

  private handleDarkModeTransition(config: LayoutConfig): void {
    const supportsViewTransition = 'startViewTransition' in document;

    if (supportsViewTransition) {
      this.startViewTransition(config);
    } else {
      this.toggleDarkMode(config);
    }
  }

  private startViewTransition(config: LayoutConfig): void {
    document.startViewTransition(() => {
      this.toggleDarkMode(config);
    });
  }

  toggleDarkMode(config?: LayoutConfig): void {
    const _config = config || this.layoutConfig();
    if (_config.darkTheme) {
      document.documentElement.classList.add('app-dark');
    } else {
      document.documentElement.classList.remove('app-dark');
    }
  }

  onMenuToggle() {
    if (this.isOverlay()) {
      this.layoutState.update((prev) => ({
        ...prev,
        overlayMenuActive: !this.layoutState().overlayMenuActive,
      }));
    }

    if (this.isDesktop()) {
      this.layoutState.update((prev) => ({
        ...prev,
        staticMenuDesktopInactive: !this.layoutState().staticMenuDesktopInactive,
      }));
    } else {
      this.layoutState.update((prev) => ({
        ...prev,
        mobileMenuActive: !this.layoutState().mobileMenuActive,
      }));
    }
  }

  isDesktop() {
    return window.innerWidth > 991;
  }
}
