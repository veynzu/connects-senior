import { HttpErrorResponse } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, DestroyRef, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';

import { Auth } from '../../core/auth';
import { NavegacionInferior } from '../../shared/navegacion-inferior';
import { Alertas, CategoriaAyuda, NuevaSolicitud, SolicitudAyuda } from './alertas';

/** PROPUESTA: duración de la cuenta regresiva (no está definida en los documentos). */
const SEGUNDOS_CUENTA = 5;

type Paso = 'categoria' | 'ubicacion' | 'cuenta' | 'enviando' | 'enviada' | 'error';

interface OpcionAyuda {
  valor: CategoriaAyuda;
  texto: string;
  icono: string;
}

interface Ubicacion {
  latitude: number;
  longitude: number;
}

@Component({
  selector: 'app-solicitar-ayuda',
  imports: [RouterLink, NavegacionInferior],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <main class="pagina">
      @switch (paso()) {
        @case ('categoria') {
          <h1>¿Necesitas ayuda?</h1>
          <p>Tus contactos de confianza recibirán una solicitud para ayudarte.</p>

          <fieldset class="categorias">
            <legend class="sr-only">¿Qué tipo de ayuda necesitas? (obligatorio)</legend>
            @for (op of opciones; track op.valor) {
              <label class="opcion" [class.elegida]="categoria() === op.valor">
                <input
                  type="radio"
                  class="sr-only"
                  name="categoria"
                  [value]="op.valor"
                  [checked]="categoria() === op.valor"
                  (change)="elegirCategoria(op.valor)"
                />
                <span class="circulo" aria-hidden="true">{{ op.icono }}</span>
                <span class="texto">{{ op.texto }}</span>
                <span class="marca" aria-hidden="true">{{
                  categoria() === op.valor ? '✓' : '›'
                }}</span>
              </label>
            }
          </fieldset>

          @if (errorCategoria()) {
            <p class="error" role="alert">Elige qué necesitas antes de pedir ayuda.</p>
          }

          <button type="button" class="boton amarillo" (click)="continuar()">
            Enviar solicitud de ayuda
          </button>
        }

        @case ('ubicacion') {
          <div class="centro">
            <span class="icono-grande" aria-hidden="true">📍</span>
            <h1>¿Quieres compartir tu ubicación?</h1>
            <p>
              Tu ubicación aproximada ayudará a tus contactos de confianza a saber dónde necesitas
              ayuda.
            </p>
          </div>
          @if (buscandoUbicacion()) {
            <p role="status" class="centro">Buscando tu ubicación…</p>
          }
          <button
            type="button"
            class="boton primario"
            [disabled]="buscandoUbicacion()"
            (click)="permitirUbicacion()"
          >
            Permitir ubicación
          </button>
          <button
            type="button"
            class="boton borde"
            [disabled]="buscandoUbicacion()"
            (click)="sinUbicacion()"
          >
            Continuar sin ubicación
          </button>
          <button
            type="button"
            class="boton borde"
            [disabled]="buscandoUbicacion()"
            (click)="paso.set('categoria')"
          >
            Volver
          </button>
        }

        @case ('cuenta') {
          <div class="centro">
            <h1>Enviando tu solicitud</h1>
            <p class="sr-only" role="status">
              Tu solicitud se enviará en {{ totalSegundos }} segundos. Presiona Cancelar para
              detenerla.
            </p>
            <p class="cuenta" aria-hidden="true">{{ segundos() }}</p>
            @if (!ubicacion()) {
              <p>Se enviará sin ubicación.</p>
            }
          </div>
          <button type="button" class="boton primario" (click)="cancelar()">Cancelar</button>
        }

        @case ('enviando') {
          <div class="centro">
            <h1>Enviando tu solicitud</h1>
            <p role="status">Un momento, por favor…</p>
          </div>
        }

        @case ('enviada') {
          <div class="centro">
            <span class="icono-grande exito" aria-hidden="true">✓</span>
            <h1>Solicitud enviada</h1>
          </div>

          @if (solicitud()?.has_trusted_contacts) {
            <p class="centro">Tus contactos de confianza han sido notificados.</p>
          } @else {
            <p class="aviso" role="alert">
              Tu solicitud quedó registrada, pero no tienes contactos de confianza configurados. Se
              avisará a tu familiar responsable.
            </p>
          }

          <dl class="detalle">
            <div>
              <dt>Fecha</dt>
              <dd>{{ fecha() }}</dd>
            </div>
            <div>
              <dt>Hora</dt>
              <dd>{{ hora() }}</dd>
            </div>
            <div>
              <dt>Estado</dt>
              <dd>{{ estadoTexto() }}</dd>
            </div>
            <div>
              <dt>Ubicación</dt>
              <dd>{{ solicitud()?.location_available ? 'Compartida' : 'No compartida' }}</dd>
            </div>
          </dl>

          <a class="boton primario" routerLink="/inicio">Volver al inicio</a>
        }

        @case ('error') {
          <div class="error" role="alert">
            <p>{{ mensajeError() }}</p>
          </div>
          <button type="button" class="boton primario" (click)="enviar()">Intentar de nuevo</button>
          <button type="button" class="boton borde" (click)="cancelar()">Volver</button>
        }
      }
    </main>

    <app-navegacion-inferior />
  `,
  styles: `
    .pagina {
      max-width: 28rem;
      margin: 0 auto;
      padding: var(--space-3) var(--space-3) 6rem;
    }
    .centro {
      text-align: center;
    }
    .categorias {
      display: grid;
      gap: var(--space-1);
      margin: 0 0 var(--space-2);
      padding: 0;
      border: 0;
    }
    .opcion {
      display: flex;
      align-items: center;
      gap: var(--space-2);
      min-height: 4.5rem;
      padding: var(--space-1) var(--space-2);
      border: 2px solid transparent;
      border-radius: var(--radius);
      background: var(--color-surface);
      box-shadow: 0 2px 8px rgba(1, 32, 79, 0.1);
      cursor: pointer;
    }
    .opcion.elegida {
      border-color: var(--color-primary);
    }
    .opcion:has(input:focus-visible) {
      outline: 3px solid var(--color-primary);
      outline-offset: 2px;
    }
    .circulo {
      display: grid;
      flex: none;
      place-items: center;
      width: 3rem;
      height: 3rem;
      border-radius: 50%;
      background: #e7ecfa;
      font-size: 1.5rem;
    }
    .texto {
      flex: 1;
      font-weight: 700;
    }
    .marca {
      color: var(--color-primary);
      font-size: 1.5rem;
    }
    .boton {
      display: flex;
      align-items: center;
      justify-content: center;
      width: 100%;
      min-height: var(--touch-target);
      margin-top: var(--space-2);
      border: 2px solid transparent;
      border-radius: var(--radius);
      font: inherit;
      font-weight: 700;
      text-decoration: none;
      cursor: pointer;
    }
    .amarillo {
      background: var(--color-warning);
      color: #1f2937;
    }
    .primario {
      background: var(--color-primary);
      color: var(--color-primary-contrast);
    }
    .borde {
      border-color: var(--color-primary);
      background: var(--color-surface);
      color: var(--color-primary);
    }
    .boton:disabled {
      opacity: 0.6;
      cursor: not-allowed;
    }
    .icono-grande {
      display: inline-grid;
      place-items: center;
      width: 4.5rem;
      height: 4.5rem;
      border-radius: 50%;
      background: #e7ecfa;
      font-size: 2rem;
    }
    .exito {
      background: var(--color-success);
      color: #fff;
      font-weight: 700;
    }
    .cuenta {
      margin: var(--space-2) 0;
      color: var(--color-primary);
      font-size: 6rem;
      font-weight: 700;
      line-height: 1;
    }
    .detalle {
      margin: var(--space-2) 0;
      padding: var(--space-1) var(--space-2);
      border-radius: var(--radius);
      background: var(--color-surface);
      box-shadow: 0 2px 8px rgba(1, 32, 79, 0.1);
    }
    .detalle div {
      display: flex;
      justify-content: space-between;
      padding: var(--space-1) 0;
    }
    .detalle div + div {
      border-top: 1px solid #e3e6ee;
    }
    dt {
      color: var(--color-heading);
    }
    dd {
      margin: 0;
      font-weight: 700;
    }
    .aviso {
      padding: var(--space-2);
      border: 2px solid #8a5a00;
      border-radius: var(--radius);
      background: #fff7e0;
      color: #4a3000;
      font-weight: 700;
    }
    .error {
      padding: var(--space-2);
      border: 2px solid #b3141b;
      border-radius: var(--radius);
      background: #fff0f0;
      color: #7a0c11;
      font-weight: 700;
    }
  `,
})
export class SolicitarAyuda {
  private readonly alertas = inject(Alertas);
  private readonly auth = inject(Auth);

  /* Categorías acordadas con el equipo a partir del mockup UI-7. */
  protected readonly opciones: readonly OpcionAyuda[] = [
    { valor: 'ayuda_en_casa', texto: 'Necesito ayuda en casa', icono: '🏠' },
    { valor: 'compania', texto: 'Necesito compañía', icono: '👥' },
    { valor: 'problema', texto: 'Tengo un problema', icono: '⚠️' },
    { valor: 'otro', texto: 'Otro', icono: '⋯' },
  ];

  protected readonly totalSegundos = SEGUNDOS_CUENTA;
  protected readonly paso = signal<Paso>('categoria');
  protected readonly categoria = signal<CategoriaAyuda | null>(null);
  /* Decisión del equipo: la categoría es obligatoria en la pantalla (el backend la acepta como opcional). */
  protected readonly errorCategoria = signal(false);
  protected readonly ubicacion = signal<Ubicacion | null>(null);
  protected readonly buscandoUbicacion = signal(false);
  protected readonly segundos = signal(SEGUNDOS_CUENTA);
  protected readonly solicitud = signal<SolicitudAyuda | null>(null);
  protected readonly mensajeError = signal('');

  private temporizador: ReturnType<typeof setInterval> | undefined;

  constructor() {
    inject(DestroyRef).onDestroy(() => this.detenerTemporizador());
  }

  protected elegirCategoria(valor: CategoriaAyuda): void {
    this.categoria.set(valor);
    this.errorCategoria.set(false);
  }

  protected continuar(): void {
    if (!this.categoria()) {
      this.errorCategoria.set(true);
      return;
    }
    this.errorCategoria.set(false);
    this.paso.set('ubicacion');
  }

  protected permitirUbicacion(): void {
    if (!('geolocation' in navigator)) {
      this.sinUbicacion();
      return;
    }
    this.buscandoUbicacion.set(true);
    navigator.geolocation.getCurrentPosition(
      (posicion) => {
        this.ubicacion.set({
          latitude: posicion.coords.latitude,
          longitude: posicion.coords.longitude,
        });
        this.buscandoUbicacion.set(false);
        this.iniciarCuenta();
      },
      () => {
        /* RF-23: si no se autoriza o no se puede obtener, la solicitud se envía igual. */
        this.buscandoUbicacion.set(false);
        this.sinUbicacion();
      },
      { enableHighAccuracy: false, timeout: 10000, maximumAge: 300000 },
    );
  }

  protected sinUbicacion(): void {
    this.ubicacion.set(null);
    this.iniciarCuenta();
  }

  protected cancelar(): void {
    this.detenerTemporizador();
    this.ubicacion.set(null);
    this.paso.set('categoria');
  }

  protected enviar(): void {
    this.paso.set('enviando');

    const cuerpo: NuevaSolicitud = {};
    const categoria = this.categoria();
    if (categoria) {
      cuerpo.category = categoria;
    }
    const ubicacion = this.ubicacion();
    if (ubicacion) {
      cuerpo.latitude = ubicacion.latitude;
      cuerpo.longitude = ubicacion.longitude;
    }

    this.alertas.crear(cuerpo).subscribe({
      next: (respuesta) => {
        this.solicitud.set(respuesta);
        this.paso.set('enviada');
      },
      error: (error: HttpErrorResponse) => {
        if (error.status === 401) {
          this.auth.cerrarSesion();
          return;
        }
        this.mensajeError.set(this.traducirError(error.status));
        this.paso.set('error');
      },
    });
  }

  protected fecha(): string {
    const solicitud = this.solicitud();
    return solicitud ? new Date(solicitud.created_at).toLocaleDateString('es-CO') : '';
  }

  protected hora(): string {
    const solicitud = this.solicitud();
    return solicitud
      ? new Date(solicitud.created_at).toLocaleTimeString('es-CO', {
        hour: '2-digit',
        minute: '2-digit',
      })
      : '';
  }

  protected estadoTexto(): string {
    const estados: Record<string, string> = {
      pendiente: 'Pendiente',
      en_atencion: 'En atención',
      atendida: 'Atendida',
    };
    const estado = this.solicitud()?.status ?? '';
    return estados[estado] ?? estado;
  }

  private iniciarCuenta(): void {
    this.detenerTemporizador();
    this.segundos.set(SEGUNDOS_CUENTA);
    this.paso.set('cuenta');
    this.temporizador = setInterval(() => {
      const restante = this.segundos() - 1;
      this.segundos.set(restante);
      if (restante <= 0) {
        this.detenerTemporizador();
        this.enviar();
      }
    }, 1000);
  }

  private detenerTemporizador(): void {
    if (this.temporizador !== undefined) {
      clearInterval(this.temporizador);
      this.temporizador = undefined;
    }
  }

  private traducirError(estado: number): string {
    switch (estado) {
      case 403:
        return 'Tu cuenta no tiene permiso para enviar solicitudes de ayuda.';
      case 503:
        return 'El servicio no está disponible ahora. Si es urgente, llama a un contacto.';
      default:
        return 'No pudimos enviar tu solicitud. Revisa tu conexión e inténtalo de nuevo. Si es urgente, llama a un contacto.';
    }
  }
}