import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  inject,
  signal,
} from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { DomSanitizer, SafeResourceUrl } from '@angular/platform-browser';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';

import { Auth } from '../../core/auth';
import {
  LlamadaEntranteDatos,
  LlamadasEntrantes,
  RUTA_LLAMADA_ENTRANTE,
  datosDeFragmento,
} from '../../core/llamadas-entrantes';

type Estado = 'timbrando' | 'activa' | 'sin-datos';

/**
 * Solo se acepta una sala https de Daily.co (la dirección llega en la notificación o en el
 * enlace); el token se agrega como parámetro "t", igual que en la llamada que hace el adulto.
 */
function direccionSegura(sala: string, token: string): string | null {
  try {
    const url = new URL(sala);
    const servidor = url.hostname.toLowerCase();
    if (url.protocol !== 'https:' || !(servidor === 'daily.co' || servidor.endsWith('.daily.co'))) {
      return null;
    }
    if (token) {
      url.searchParams.set('t', token);
    }
    return url.toString();
  } catch {
    return null;
  }
}

/** La notificación abre esta llamada: su dirección (onActionClick del backend) es su ruta. */
function abreLaLlamada(notificacion: Notification, callId: string): boolean {
  const url = String(notificacion.data?.onActionClick?.default?.url ?? '');
  if (!url || !callId) {
    return false;
  }
  try {
    const ruta = new URL(url, window.location.origin).pathname.replace(/\/$/, '');
    return ruta === `${RUTA_LLAMADA_ENTRANTE}/${encodeURIComponent(callId)}`;
  } catch {
    return false;
  }
}

/**
 * Cierra las notificaciones del sistema de esta llamada. Si la persona tocara una durante la
 * llamada, el service worker recargaría la página y la cortaría. Sin service worker no hace nada.
 */
async function cerrarNotificacionesDe(callId: string): Promise<void> {
  try {
    const registro = await navigator.serviceWorker?.getRegistration?.();
    const lista = (await registro?.getNotifications?.()) ?? [];
    lista.filter((notificacion) => abreLaLlamada(notificacion, callId)).forEach((n) => n.close());
  } catch {
    /* Sin permiso o sin service worker: la notificación queda, la llamada sigue. */
  }
}

/**
 * Pantalla de quien recibe la llamada (el contacto). No llama al backend: el estado de la
 * llamada (L2) solo lo actualiza el adulto mayor que llamó.
 */
@Component({
  selector: 'app-llamada-entrante',
  imports: [RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <main class="pantalla">
      @switch (estado()) {
        @case ('timbrando') {
          <h1>{{ titulo() }}</h1>
          <p role="status" class="mensaje">
            {{ datos()?.audio_only ? 'Llamada de voz entrante' : 'Videollamada entrante' }}
          </p>
          <button type="button" class="contestar" (click)="contestar()">Contestar</button>
          <button type="button" class="rechazar" (click)="salir()">Rechazar</button>
        }
        @case ('activa') {
          <h1>{{ datos()?.audio_only ? 'Llamada con' : 'Videollamada con' }} {{ nombre() }}</h1>
          @if (sala(); as url) {
            <iframe
              class="sala"
              [src]="url"
              title="Sala de la llamada"
              allow="camera; microphone; autoplay; display-capture; fullscreen"
            ></iframe>
          }
          <p role="status" class="mensaje">Comunicación en curso</p>
          <button type="button" class="rechazar" (click)="salir()">Colgar</button>
        }
        @case ('sin-datos') {
          <h1>Llamada</h1>
          <p role="alert" class="mensaje">La llamada ya no está disponible.</p>
          <a class="volver" [routerLink]="rutaInicial">Volver</a>
        }
      }
    </main>
  `,
  styles: `
    :host {
      display: block;
      min-height: 100vh;
      background: #071a45;
      color: #fff;
    }
    .pantalla {
      max-width: 28rem;
      margin: 0 auto;
      padding: var(--space-3);
    }
    h1 {
      color: #fff;
      text-align: center;
    }
    .mensaje {
      text-align: center;
      font-weight: 700;
    }
    .sala {
      display: block;
      width: 100%;
      height: 60vh;
      min-height: 20rem;
      border: 0;
      border-radius: var(--radius);
      background: #000;
    }
    .contestar,
    .rechazar,
    .volver {
      display: flex;
      align-items: center;
      justify-content: center;
      width: 100%;
      min-height: calc(var(--touch-target) + 1rem);
      margin-top: var(--space-2);
      border: 2px solid transparent;
      border-radius: var(--radius);
      font: inherit;
      font-size: 1.25rem;
      font-weight: 700;
      text-decoration: none;
      cursor: pointer;
    }
    .contestar {
      background: var(--color-success, #1b7f3b);
      color: #fff;
    }
    .rechazar {
      background: var(--color-danger);
      color: #fff;
    }
    .volver {
      border-color: #fff;
      background: transparent;
      color: #fff;
    }
  `,
})
export class LlamadaEntrante {
  private readonly ruta = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly auth = inject(Auth);
  private readonly llamadas = inject(LlamadasEntrantes);
  private readonly sanitizer = inject(DomSanitizer);

  protected readonly estado = signal<Estado>('sin-datos');
  protected readonly datos = signal<LlamadaEntranteDatos | null>(null);
  protected readonly sala = signal<SafeResourceUrl | null>(null);
  protected readonly rutaInicial = this.auth.rutaInicial();

  protected readonly nombre = computed(() => this.datos()?.from_name.trim() ?? '');
  protected readonly titulo = computed(() => {
    const nombre = this.nombre();
    if (this.datos()?.audio_only) {
      return `${nombre || 'Alguien'} te está llamando`;
    }
    return nombre ? `Videollamada de ${nombre}` : 'Videollamada entrante';
  });

  private direccion: string | null = null;
  /** call_id de la ruta: identifica las notificaciones de esta llamada. */
  private callId = '';

  constructor() {
    // Si llega otra llamada mientras la pantalla timbra, la ruta cambia y se vuelve a leer.
    this.ruta.paramMap
      .pipe(takeUntilDestroyed())
      .subscribe((parametros) => this.cargar(parametros.get('id') ?? ''));
    inject(DestroyRef).onDestroy(() => this.llamadas.enLlamada.set(false));
  }

  protected contestar(): void {
    if (!this.direccion) {
      this.estado.set('sin-datos');
      return;
    }
    this.sala.set(this.sanitizer.bypassSecurityTrustResourceUrl(this.direccion));
    this.llamadas.enLlamada.set(true);
    this.estado.set('activa');
    void cerrarNotificacionesDe(this.callId);
  }

  /** Rechazar o colgar: vuelve a la pantalla inicial de la persona. */
  protected salir(): void {
    this.sala.set(null);
    this.llamadas.terminar();
    void cerrarNotificacionesDe(this.callId);
    void this.router.navigateByUrl(this.auth.rutaInicial());
  }

  /** Datos del servicio (push con la app abierta) o del fragmento de la dirección. */
  private cargar(callId: string): void {
    const guardada = this.llamadas.actual();
    const datos =
      guardada && guardada.call_id === callId
        ? guardada
        : datosDeFragmento(callId, this.ruta.snapshot.fragment);
    const direccion = datos ? direccionSegura(datos.room_url, datos.token) : null;

    this.llamadas.enLlamada.set(false);
    this.sala.set(null);
    this.callId = callId;
    this.direccion = direccion;
    this.datos.set(direccion ? datos : null);
    this.estado.set(direccion ? 'timbrando' : 'sin-datos');
  }
}
