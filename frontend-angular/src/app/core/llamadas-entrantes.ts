import { Injectable, inject, signal } from '@angular/core';
import { Router } from '@angular/router';
import { SwPush } from '@angular/service-worker';

/*
 * Llamadas entrantes: el backend envía una push cuando un adulto mayor llama a un contacto que
 * tiene cuenta (L1). Su `data` trae type = 'call', call_id, room_url, token, audio_only
 * ('true' o 'false') y from_name, todo como texto.
 * - Con la app abierta, la push llega por SwPush.messages y se abre la pantalla de la llamada.
 * - Con la app cerrada, el service worker muestra la notificación; al tocarla abre
 *   /llamadas/entrante/<call_id>#room=…&t=…&audio=1|0&from=… (dirección que arma el backend).
 */

export interface LlamadaEntranteDatos {
  call_id: string;
  room_url: string;
  token: string;
  audio_only: boolean;
  from_name: string;
}

/** Ruta de la pantalla de llamada entrante (la misma que abre la notificación). */
export const RUTA_LLAMADA_ENTRANTE = '/llamadas/entrante';

type Datos = Record<string, unknown>;

function texto(valor: unknown): string {
  return typeof valor === 'string' ? valor : '';
}

function esLlamada(valor: unknown): valor is Datos {
  return typeof valor === 'object' && valor !== null && (valor as Datos)['type'] === 'call';
}

/** Datos de la llamada dentro de la push (en `data`, en `notification.data` o en la raíz). */
function datosDePush(payload: unknown): LlamadaEntranteDatos | null {
  const p = payload as { data?: unknown; notification?: { data?: unknown } } | null;
  const d = [p?.data, p?.notification?.data, p].find(esLlamada);
  if (!d) {
    return null;
  }
  const callId = texto(d['call_id']);
  const sala = texto(d['room_url']);
  if (!callId || !sala) {
    return null;
  }
  const audio = texto(d['audio_only']);
  return {
    call_id: callId,
    room_url: sala,
    token: texto(d['token']),
    audio_only: audio === 'true' || audio === '1',
    from_name: texto(d['from_name']),
  };
}

/** Lee room, t, audio y from del fragmento de la dirección (#room=…&t=…&audio=…&from=…). */
export function datosDeFragmento(
  callId: string,
  fragmento: string | null,
): LlamadaEntranteDatos | null {
  if (!callId || !fragmento) {
    return null;
  }
  const parametros = new URLSearchParams(fragmento.replace(/^#/, ''));
  const sala = parametros.get('room') ?? '';
  if (!sala) {
    return null;
  }
  const audio = parametros.get('audio');
  return {
    call_id: callId,
    room_url: sala,
    token: parametros.get('t') ?? '',
    audio_only: audio === '1' || audio === 'true',
    from_name: parametros.get('from') ?? '',
  };
}

function fragmentoDe(datos: LlamadaEntranteDatos): string {
  return new URLSearchParams({
    room: datos.room_url,
    t: datos.token,
    audio: datos.audio_only ? '1' : '0',
    from: datos.from_name,
  }).toString();
}

@Injectable({ providedIn: 'root' })
export class LlamadasEntrantes {
  /** Solo existe con el service worker activo (build de producción). */
  private readonly swPush = inject(SwPush, { optional: true });
  private readonly router = inject(Router);

  /** Última llamada recibida; la pantalla la usa si coincide el call_id de la ruta. */
  readonly actual = signal<LlamadaEntranteDatos | null>(null);
  /** Verdadero mientras hay una llamada contestada: otra llamada no la interrumpe. */
  readonly enLlamada = signal(false);

  private iniciado = false;

  /** Se llama al arrancar la app. */
  iniciar(): void {
    if (this.iniciado) {
      return;
    }
    this.iniciado = true;
    this.leerDireccionInicial();
    if (!this.swPush?.isEnabled) {
      return;
    }
    // Al tocar la notificación no hace falta nada aquí: el service worker abre la pantalla.
    this.swPush.messages.subscribe((payload) => this.recibir(payload));
  }

  /** Al colgar o rechazar se olvida la llamada. */
  terminar(): void {
    this.actual.set(null);
    this.enLlamada.set(false);
  }

  private recibir(payload: object): void {
    const datos = datosDePush(payload);
    if (!datos) {
      return;
    }
    this.actual.set(datos);
    // En otra llamada (hecha o contestada) no se cambia de pantalla: queda la notificación.
    if (this.enLlamada() || this.router.url.startsWith('/llamada/')) {
      return;
    }
    void this.router.navigate([RUTA_LLAMADA_ENTRANTE, datos.call_id], {
      fragment: fragmentoDe(datos),
    });
  }

  /**
   * Si la app se abrió desde la notificación, la dirección trae la llamada en el fragmento.
   * Se lee aquí, antes de que el router la procese: el router decodifica el fragmento completo
   * y un nombre con "&" o "=" se cortaría.
   */
  private leerDireccionInicial(): void {
    if (typeof window === 'undefined') {
      return;
    }
    const prefijo = `${RUTA_LLAMADA_ENTRANTE}/`;
    const ruta = window.location.pathname;
    if (!ruta.startsWith(prefijo)) {
      return;
    }
    let callId: string;
    try {
      callId = decodeURIComponent(ruta.slice(prefijo.length).replace(/\/$/, ''));
    } catch {
      return;
    }
    const datos = datosDeFragmento(callId, window.location.hash);
    if (datos) {
      this.actual.set(datos);
    }
  }
}
