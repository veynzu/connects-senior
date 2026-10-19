import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  inject,
  signal,
  viewChild,
} from '@angular/core';
import { Router } from '@angular/router';

import {
  Notificacion,
  Notificaciones,
  TipoNotificacion,
  horaRelativa,
} from '../core/notificaciones';

/** Cuántas notificaciones del historial se muestran en el panel (las más recientes). */
const MAXIMO_VISIBLES = 50;

const ICONOS: Record<TipoNotificacion, string> = {
  alerta: '🤝',
  recordatorio: '⏰',
  mensaje_voz: '🎤',
  llamada: '📞',
};

/**
 * Campana de la cabecera superior con el número de notificaciones sin leer.
 * El panel muestra el historial: las no vistas resaltadas y las vistas con el estilo normal.
 * Al cerrarlo (o al tocar un elemento) todo lo mostrado queda como visto y sigue en la lista.
 * El panel se ancla al borde inferior del contenedor posicionado más cercano (la cabecera).
 */
@Component({
  selector: 'app-campana-notificaciones',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: {
    '(document:click)': 'cerrarSiEsFuera($event)',
    '(document:keydown.escape)': 'cerrarConTeclado()',
  },
  template: `
    <button
      #boton
      type="button"
      class="campana"
      aria-label="Notificaciones"
      aria-describedby="conteo-notificaciones"
      [attr.aria-expanded]="abierto()"
      [attr.aria-controls]="abierto() ? 'panel-notificaciones' : null"
      (click)="alternar()"
    >
      <svg class="icono-campana" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
        <path
          d="M12 3a6 6 0 0 0-6 6v4.5l-1.8 2.7a.8.8 0 0 0 .7 1.3h14.2a.8.8 0 0 0 .7-1.3L18 13.5V9a6 6 0 0 0-6-6Z"
        />
        <path d="M9.8 20.5a2.4 2.4 0 0 0 4.4 0" />
      </svg>
      @if (servicio.noLeidas() > 0) {
        <span class="distintivo" aria-hidden="true">{{ distintivo() }}</span>
      }
    </button>
    <span id="conteo-notificaciones" class="sr-only" aria-live="polite">{{ conteo() }}</span>

    @if (abierto()) {
      <section id="panel-notificaciones" class="panel" aria-labelledby="titulo-notificaciones">
        <div class="cabecera-panel">
          <h2 id="titulo-notificaciones">Notificaciones</h2>
          <button
            type="button"
            class="marcar"
            [disabled]="servicio.noLeidas() === 0"
            (click)="servicio.marcarTodasLeidas()"
          >
            Marcar todas como leídas
          </button>
        </div>

        @if (visibles().length === 0) {
          <p class="vacio">Sin notificaciones</p>
        } @else {
          <ul class="lista">
            @for (n of visibles(); track n.id) {
              <li>
                <button
                  type="button"
                  class="elemento"
                  [class.no-leida]="servicio.esNoLeida(n.id)"
                  [class.cerrada]="n.estado === 'cerrada'"
                  [class.urgente]="n.tipo === 'alerta' && n.estado === 'activa'"
                  (click)="abrir(n)"
                >
                  <span class="icono" aria-hidden="true">{{ icono(n) }}</span>
                  <span class="textos">
                    <span class="titulo">{{ n.titulo }}</span>
                    <span>{{ n.detalle }}</span>
                    <span class="hora">{{ hora(n) }}</span>
                  </span>
                  @if (servicio.esNoLeida(n.id)) {
                    <span class="punto" aria-hidden="true"></span>
                    <span class="sr-only">Sin leer</span>
                  }
                </button>
              </li>
            }
          </ul>
        }
      </section>
    }
  `,
  styles: `
    .campana {
      position: relative;
      display: grid;
      place-items: center;
      width: var(--touch-target);
      height: var(--touch-target);
      border: 0;
      border-radius: 50%;
      background: transparent;
      color: inherit;
      cursor: pointer;
    }
    .campana:hover,
    .campana[aria-expanded='true'] {
      background: rgba(255, 255, 255, 0.18);
    }
    .icono-campana {
      width: 1.875rem;
      height: 1.875rem;
      fill: none;
      stroke: currentColor;
      stroke-width: 2;
      stroke-linecap: round;
      stroke-linejoin: round;
    }
    .distintivo {
      position: absolute;
      top: 0;
      right: 0;
      min-width: 1.75rem;
      height: 1.75rem;
      padding: 0 0.3rem;
      border: 2px solid var(--color-primary-contrast);
      border-radius: 999px;
      background: var(--color-danger);
      color: var(--color-danger-contrast);
      font-size: 1rem;
      font-weight: 700;
      line-height: 1.5rem;
      text-align: center;
    }
    .panel {
      position: absolute;
      top: 100%;
      right: 0;
      width: min(26rem, 100vw);
      max-height: min(75vh, 40rem);
      overflow-y: auto;
      border: 1px solid #d5d9e3;
      border-radius: var(--radius);
      background: var(--color-surface);
      color: var(--color-text);
      box-shadow: 0 8px 24px rgba(1, 32, 79, 0.25);
    }
    .cabecera-panel {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 0 var(--space-1);
      padding: var(--space-1) var(--space-2);
      border-bottom: 1px solid #e3e6ee;
    }
    h2 {
      margin: 0;
      font-size: 1.25rem;
    }
    .marcar {
      min-height: 3rem;
      padding: 0 0.25rem;
      border: 0;
      background: transparent;
      color: var(--color-primary);
      font: inherit;
      font-size: 1rem;
      font-weight: 700;
      text-decoration: underline;
      cursor: pointer;
    }
    .marcar:disabled {
      color: #4b5563;
      text-decoration: none;
      cursor: default;
    }
    .vacio {
      margin: 0;
      padding: var(--space-3) var(--space-2);
      text-align: center;
    }
    .lista {
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .lista li + li {
      border-top: 1px solid #e3e6ee;
    }
    .elemento {
      display: flex;
      align-items: flex-start;
      gap: var(--space-2);
      width: 100%;
      min-height: 4rem;
      padding: var(--space-2);
      border: 0;
      border-left: 6px solid transparent;
      background: transparent;
      color: inherit;
      font: inherit;
      font-size: 1rem;
      text-align: left;
      cursor: pointer;
    }
    .elemento:hover {
      background: #f1f4fa;
    }
    .no-leida {
      background: #eaf0fc;
    }
    .urgente {
      border-left-color: var(--color-danger);
    }
    .icono {
      font-size: 1.5rem;
      line-height: 1.75rem;
    }
    .textos {
      display: grid;
      flex: 1;
      gap: 0.125rem;
    }
    .titulo {
      color: var(--color-heading);
      font-size: 1.125rem;
      font-weight: 700;
    }
    .hora {
      color: #4b5563;
    }
    .cerrada .hora {
      color: #5f6673;
    }
    .punto {
      flex: none;
      width: 0.875rem;
      height: 0.875rem;
      margin-top: 0.4rem;
      border-radius: 50%;
      background: var(--color-primary);
    }
    @media (max-width: 30rem) {
      .panel {
        left: 0;
        width: auto;
        border-radius: 0 0 var(--radius) var(--radius);
      }
    }
  `,
})
export class CampanaNotificaciones {
  private readonly elemento: ElementRef<HTMLElement> = inject(ElementRef);
  private readonly router = inject(Router);
  protected readonly servicio = inject(Notificaciones);

  private readonly boton = viewChild.required<ElementRef<HTMLButtonElement>>('boton');

  protected readonly abierto = signal(false);

  protected readonly visibles = computed(() => this.servicio.items().slice(0, MAXIMO_VISIBLES));

  protected readonly distintivo = computed(() => {
    const cantidad = this.servicio.noLeidas();
    return cantidad > 9 ? '9+' : String(cantidad);
  });

  protected readonly conteo = computed(() => {
    const cantidad = this.servicio.noLeidas();
    if (cantidad === 0) {
      return 'No tienes notificaciones sin leer';
    }
    return cantidad === 1 ? '1 notificación sin leer' : `${cantidad} notificaciones sin leer`;
  });

  protected alternar(): void {
    if (this.abierto()) {
      this.cerrar();
      return;
    }
    this.abierto.set(true);
    this.servicio.actualizar();
  }

  protected abrir(notificacion: Notificacion): void {
    this.servicio.marcarLeida(notificacion.id);
    this.cerrar();
    this.router.navigateByUrl(notificacion.ruta);
  }

  protected cerrarSiEsFuera(evento: Event): void {
    const destino = evento.target;
    if (
      this.abierto() &&
      destino instanceof Node &&
      !this.elemento.nativeElement.contains(destino)
    ) {
      this.cerrar();
    }
  }

  protected cerrarConTeclado(): void {
    if (!this.abierto()) {
      return;
    }
    this.cerrar();
    this.boton().nativeElement.focus();
  }

  protected icono(notificacion: Notificacion): string {
    return ICONOS[notificacion.tipo];
  }

  protected hora(notificacion: Notificacion): string {
    return horaRelativa(notificacion.fecha);
  }

  /** Cierra el panel: lo que se mostró ya se vio (queda en la lista sin resaltar). */
  private cerrar(): void {
    this.abierto.set(false);
    this.servicio.marcarTodasLeidas();
  }
}
