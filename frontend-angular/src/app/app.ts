import { Component, computed, inject, signal } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { Event, NavigationEnd, Router, RouterOutlet } from '@angular/router';
import { filter, map } from 'rxjs';

import { Auth } from './core/auth';
import { CampanaNotificaciones } from './shared/campana-notificaciones';

/**
 * Pantallas sin cabecera superior: las de entrar o crear cuenta, la prueba de conexión y la
 * llamada en curso (salir de ella desde la campana dejaría la llamada sin cerrar).
 */
const RUTAS_SIN_CABECERA = ['/login', '/registro', '/recuperar', '/prueba-conexion'];
const PREFIJOS_SIN_CABECERA = ['/llamada/'];

@Component({
  selector: 'app-root',
  imports: [RouterOutlet, CampanaNotificaciones],
  templateUrl: './app.html',
  styleUrl: './app.css',
})
export class App {
  protected readonly title = signal('frontend-angular');

  private readonly auth = inject(Auth);
  private readonly router = inject(Router);

  /** Ruta actual sin parámetros; null hasta que termina la primera navegación. */
  private readonly ruta = toSignal(
    this.router.events.pipe(
      filter((evento: Event): evento is NavigationEnd => evento instanceof NavigationEnd),
      map((evento) => evento.urlAfterRedirects.split(/[?#]/)[0]),
    ),
    { initialValue: null },
  );

  /** La cabecera con la campana se muestra en todas las pantallas con sesión. */
  protected readonly mostrarCabecera = computed(() => {
    const ruta = this.ruta();
    if (!this.auth.usuario() || ruta === null) {
      return false;
    }
    return (
      !RUTAS_SIN_CABECERA.includes(ruta) &&
      !PREFIJOS_SIN_CABECERA.some((prefijo) => ruta.startsWith(prefijo))
    );
  });
}
