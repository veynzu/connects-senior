import { HttpClient } from '@angular/common/http';
import { Injectable, Injector, computed, effect, inject, signal, untracked } from '@angular/core';
import { SwPush } from '@angular/service-worker';
import { firstValueFrom } from 'rxjs';

import { environment } from '../../environments/environment';
import { API_BASE_URL } from './api-config';
import { Auth } from './auth';

/*
 * Notificaciones push (contrato v1.0, A10: POST /api/auth/devices con { fcm_token }).
 * Firebase Cloud Messaging entrega el token usando el service worker de Angular (ngsw), que es
 * quien recibe las push y muestra la notificación aunque la app esté cerrada.
 * El SDK de Firebase se carga aparte (import dinámico) para no agrandar la carga inicial.
 * Nunca se escribe el token en la consola.
 */

export type PermisoPush = NotificationPermission | 'no-soportado';

/**
 * Último registro hecho desde este equipo. Es una sola clave por equipo (no una por persona):
 * el backend asigna el token a la última persona que lo registró, así que en un equipo
 * compartido hay que volver a enviarlo cada vez que entra otra persona.
 */
interface TokenGuardado {
  /** Persona a la que quedó asignado el token en el backend. */
  uid: string;
  token: string;
  /** Momento (ms) del último envío al backend. */
  fecha: number;
}

const CLAVE_TOKEN = 'cs.push.token';

/**
 * El token se vuelve a enviar al backend si cambió la persona, si cambió el token o si pasó un
 * día desde el último envío.
 */
const VIGENCIA_REGISTRO_MS = 24 * 60 * 60 * 1000;

function leerPermiso(): PermisoPush {
  return typeof Notification === 'undefined' ? 'no-soportado' : Notification.permission;
}

/** iPhone o iPad con la app abierta en Safari (sin instalar): allí no hay notificaciones. */
function detectarIphoneSinInstalar(): boolean {
  if (typeof navigator === 'undefined' || typeof window === 'undefined') {
    return false;
  }
  const instalada =
    (navigator as Navigator & { standalone?: boolean }).standalone === true ||
    window.matchMedia?.('(display-mode: standalone)').matches === true;
  return /(iPhone|iPad|iPod)/.test(navigator.userAgent) && !instalada;
}

/** Código del error para la consola, sin el token ni datos de la persona. */
function codigoError(error: unknown): string {
  const conCodigo = error as { code?: unknown; name?: unknown } | null;
  if (typeof conCodigo?.code === 'string') {
    return conCodigo.code;
  }
  return typeof conCodigo?.name === 'string' ? conCodigo.name : 'desconocido';
}

@Injectable({ providedIn: 'root' })
export class Push {
  private readonly http = inject(HttpClient);
  private readonly auth = inject(Auth);
  private readonly injector = inject(Injector);
  /** El service worker solo está activo en el build de producción (provideServiceWorker). */
  private readonly swActivo = inject(SwPush, { optional: true })?.isEnabled ?? false;

  private readonly compatible = signal(false);
  /** El navegador puede recibir push: Firebase, service worker y notificaciones disponibles. */
  readonly soportado = this.compatible.asReadonly();
  readonly permiso = signal<PermisoPush>(leerPermiso());
  readonly esIphoneSinInstalar = detectarIphoneSinInstalar();
  /** "Ahora no" en el aviso: lo oculta hasta que se recarga la página. */
  readonly avisoOculto = signal(false);

  private readonly uid = computed(() => this.auth.usuario()?.uid ?? null);

  private comprobacion: Promise<boolean> | null = null;
  /** Los registros se hacen de a uno para no enviar el mismo token dos veces a la vez. */
  private cola: Promise<void> = Promise.resolve();
  private iniciado = false;

  /** Se llama al arrancar la app: registra el token cada vez que entra una persona. */
  iniciar(): void {
    if (this.iniciado) {
      return;
    }
    this.iniciado = true;
    void this.comprobarSoporte();
    this.vigilarPermiso();
    effect(
      () => {
        const uid = this.uid();
        if (uid) {
          untracked(() => void this.registrarSiYaPermitido());
        }
      },
      { injector: this.injector },
    );
  }

  /** Pide el permiso del navegador. Solo desde un clic: los navegadores lo exigen. */
  async activar(): Promise<void> {
    if (!this.compatible()) {
      return;
    }
    let permiso: NotificationPermission;
    try {
      permiso = await Notification.requestPermission();
    } catch (error) {
      console.warn('No se pudo pedir el permiso de notificaciones:', codigoError(error));
      return;
    }
    this.permiso.set(permiso);
    if (permiso === 'granted') {
      await this.registrarToken();
    }
  }

  /** Con sesión y permiso ya concedido, envía el token al backend si hace falta. */
  async registrarSiYaPermitido(): Promise<void> {
    if (!(await this.comprobarSoporte())) {
      return;
    }
    const permiso = leerPermiso();
    this.permiso.set(permiso);
    if (permiso === 'granted') {
      await this.registrarToken();
    }
  }

  ocultarAviso(): void {
    this.avisoOculto.set(true);
  }

  private comprobarSoporte(): Promise<boolean> {
    if (!this.comprobacion) {
      this.comprobacion = this.calcularSoporte().then((valor) => {
        this.compatible.set(valor);
        return valor;
      });
    }
    return this.comprobacion;
  }

  private async calcularSoporte(): Promise<boolean> {
    if (
      !this.swActivo ||
      typeof window === 'undefined' ||
      !('serviceWorker' in navigator) ||
      !('Notification' in window)
    ) {
      return false;
    }
    try {
      const { isSupported } = await import('firebase/messaging');
      return await isSupported();
    } catch (error) {
      console.warn('No se pudo comprobar si hay notificaciones:', codigoError(error));
      return false;
    }
  }

  /** Si la persona cambia el permiso en la configuración del sitio, el aviso se actualiza. */
  private vigilarPermiso(): void {
    if (typeof navigator === 'undefined' || !navigator.permissions?.query) {
      return;
    }
    navigator.permissions
      .query({ name: 'notifications' })
      .then((estado) => {
        estado.onchange = () => void this.registrarSiYaPermitido();
      })
      .catch(() => undefined);
  }

  private registrarToken(): Promise<void> {
    this.cola = this.cola.then(() => this.obtenerYEnviarToken());
    return this.cola;
  }

  /** getToken con el service worker de Angular y, si cambió o pasó un día, A10. Nunca lanza. */
  private async obtenerYEnviarToken(): Promise<void> {
    const uid = this.uid();
    if (!uid) {
      return;
    }
    try {
      const [{ getApps, initializeApp }, { getMessaging, getToken }] = await Promise.all([
        import('firebase/app'),
        import('firebase/messaging'),
      ]);
      const app = getApps().length ? getApps()[0] : initializeApp(environment.firebase);
      const registro = await navigator.serviceWorker.ready;
      const token = await getToken(getMessaging(app), {
        serviceWorkerRegistration: registro,
        ...(environment.fcmVapidKey ? { vapidKey: environment.fcmVapidKey } : {}),
      });
      // Si otra persona entró mientras tanto, su propio registro se encarga.
      if (!token || this.uid() !== uid || this.yaEnviado(uid, token)) {
        return;
      }
      await firstValueFrom(
        this.http.post<void>(`${API_BASE_URL}/api/auth/devices`, { fcm_token: token }),
      );
      this.guardarToken(uid, token);
    } catch (error) {
      console.warn('No se pudieron activar las notificaciones en este equipo:', codigoError(error));
    }
  }

  private yaEnviado(uid: string, token: string): boolean {
    try {
      const texto = localStorage.getItem(CLAVE_TOKEN);
      const guardado = texto ? (JSON.parse(texto) as Partial<TokenGuardado> | null) : null;
      if (guardado?.uid !== uid || guardado.token !== token || typeof guardado.fecha !== 'number') {
        return false;
      }
      const edad = Date.now() - guardado.fecha;
      return edad >= 0 && edad < VIGENCIA_REGISTRO_MS;
    } catch {
      return false;
    }
  }

  private guardarToken(uid: string, token: string): void {
    const guardado: TokenGuardado = { uid, token, fecha: Date.now() };
    try {
      localStorage.setItem(CLAVE_TOKEN, JSON.stringify(guardado));
    } catch {
      /* Sin almacenamiento disponible: se vuelve a enviar la próxima vez. */
    }
  }
}
