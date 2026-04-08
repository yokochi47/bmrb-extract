import { bootstrapApplication } from '@angular/platform-browser';
import { appConfig } from './app.config';
import { app } from './app';

bootstrapApplication(app, appConfig).catch((err) => console.error(err));
