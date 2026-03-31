import { ApplicationConfig, provideBrowserGlobalErrorListeners } from '@angular/core';
import { provideRouter } from '@angular/router';

import { routes } from './app.routes';

import { providePrimeNG } from 'primeng/config';
import Aura from '@primeuix/themes/aura'; // Import a theme preset, e.g., Aura

export const appConfig: ApplicationConfig = {
  providers: [
    provideBrowserGlobalErrorListeners(),
    provideRouter(routes),
    // Configure the default theme preset
    providePrimeNG({
      theme: {
        preset: Aura
      }
    })
  ]
};
