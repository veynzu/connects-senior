import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';

import { Auth } from '../core/auth';
import { Push } from '../core/push';

type Caso = 'activar' | 'bloqueadas' | 'iphone';

/**
 * Aviso bajo la cabecera para activar las notificaciones push: sin ellas el contacto no se
 * entera de las llamadas. Se oculta cuando el permiso ya está concedido.
 */
@Component({
  selector: 'app-aviso-notificaciones',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (caso(); as tipo) {
      <section class="aviso" aria-label="Aviso de notificaciones">
        @switch (tipo) {
          @case ('activar') {
            <p>Activa las notificaciones para recibir llamadas y avisos.</p>
          }
          @case ('bloqueadas') {
            <p>
              Las notificaciones están bloqueadas en tu navegador. Actívalas en la configuración del
              sitio para recibir llamadas.
            </p>
          }
          @case ('iphone') {
            <p>
              Para recibir llamadas en iPhone: toca Compartir y luego Añadir a pantalla de inicio, y
              abre la app desde ahí.
            </p>
          }
        }
        <div class="botones">
          @if (tipo === 'activar') {
            <button type="button" class="activar" [disabled]="activando()" (click)="activar()">
              Activar
            </button>
          }
          <button type="button" class="despues" (click)="push.ocultarAviso()">Ahora no</button>
        </div>
      </section>
    }
  `,
  styles: `
    .aviso {
      max-width: 28rem;
      margin: var(--space-2) auto 0;
      padding: var(--space-2);
      border: 2px solid var(--color-primary);
      border-radius: var(--radius);
      background: var(--color-surface);
      color: var(--color-heading);
    }
    p {
      margin: 0 0 var(--space-2);
      font-weight: 700;
    }
    .botones {
      display: flex;
      flex-wrap: wrap;
      gap: var(--space-1);
    }
    button {
      flex: 1 1 8rem;
      min-height: var(--touch-target);
      padding: 0 var(--space-2);
      border: 2px solid var(--color-primary);
      border-radius: var(--radius);
      font: inherit;
      font-weight: 700;
      cursor: pointer;
    }
    .activar {
      background: var(--color-primary);
      color: var(--color-primary-contrast);
    }
    .activar:disabled {
      opacity: 0.6;
      cursor: wait;
    }
    .despues {
      background: var(--color-surface);
      color: var(--color-primary);
    }
  `,
})
export class AvisoNotificaciones {
  protected readonly push = inject(Push);
  private readonly auth = inject(Auth);

  protected readonly activando = signal(false);

  protected readonly caso = computed<Caso | null>(() => {
    if (!this.auth.usuario() || this.push.avisoOculto()) {
      return null;
    }
    const permiso = this.push.permiso();
    if (permiso === 'granted') {
      return null;
    }
    if (this.push.esIphoneSinInstalar) {
      return 'iphone';
    }
    if (permiso === 'denied') {
      return 'bloqueadas';
    }
    return permiso === 'default' && this.push.soportado() ? 'activar' : null;
  });

  protected activar(): void {
    this.activando.set(true);
    void this.push.activar().finally(() => this.activando.set(false));
  }
}
