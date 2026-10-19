import {
  ApplicationConfig,
  inject,
  isDevMode,
  provideAppInitializer,
  provideBrowserGlobalErrorListeners,
} from '@angular/core';
import { provideHttpClient, withInterceptors } from '@angular/common/http';
import { provideRouter } from '@angular/router';
import { provideServiceWorker } from '@angular/service-worker';

import { routes } from './app.routes';
import { Auth } from './core/auth';
import { authInterceptor } from './core/auth-interceptor';
import { LlamadasEntrantes } from './core/llamadas-entrantes';
import { Notificaciones } from './core/notificaciones';
import { Push } from './core/push';

export const appConfig: ApplicationConfig = {
  providers: [
    provideBrowserGlobalErrorListeners(),
    provideHttpClient(withInterceptors([authInterceptor])),
    provideRouter(routes),
    provideAppInitializer(() => {
      inject(Auth).recuperarUsuario();
      inject(Notificaciones).iniciar();
      inject(Push).iniciar();
      inject(LlamadasEntrantes).iniciar();
    }),
    provideServiceWorker('ngsw-worker.js', {
      enabled: !isDevMode(),
      // Inmediato: las notificaciones push necesitan el service worker activo desde el inicio.
      registrationStrategy: 'registerImmediately',
    }),
  ],
};
