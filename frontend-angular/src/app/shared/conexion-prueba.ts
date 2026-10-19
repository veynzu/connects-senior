import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { ApiStatus } from '../core/api-status';

type Estado = 'inicial' | 'cargando' | 'ok' | 'error';

@Component({
  selector: 'app-conexion-prueba',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section class="conexion">
      <h2>Conexión con el servidor</h2>
      <p>Prueba temporal del endpoint <code>GET /</code> del backend.</p>

      <button
        type="button"
        class="boton"
        (click)="verificar()"
        [disabled]="estado() === 'cargando'"
      >
        Probar conexión
      </button>

      <p class="resultado" role="status" aria-live="polite">
        @switch (estado()) {
          @case ('cargando') {
            Conectando con el servidor…
          }
          @case ('ok') {
            Servidor respondió: {{ mensaje() }}
          }
          @case ('error') {
            No pudimos conectar con el servidor. Revisa que esté encendido e inténtalo de nuevo.
          }
        }
      </p>
    </section>
  `,
  styles: `
    .conexion {
      max-width: 28rem;
      margin: 0 auto;
      padding: var(--space-3);
    }
    .boton {
      width: 100%;
      min-height: var(--touch-target);
      border: 0;
      border-radius: var(--radius);
      background: var(--color-primary);
      color: var(--color-primary-contrast);
      font: inherit;
      font-weight: 700;
      cursor: pointer;
    }
    .boton:disabled {
      opacity: 0.6;
      cursor: wait;
    }
    .resultado {
      margin-top: var(--space-2);
      font-weight: 700;
    }
  `,
})
export class ConexionPrueba {
  private readonly apiStatus = inject(ApiStatus);

  protected readonly estado = signal<Estado>('inicial');
  protected readonly mensaje = signal('');

  protected verificar(): void {
    this.estado.set('cargando');
    this.apiStatus.consultar().subscribe({
      next: (respuesta) => {
        this.mensaje.set(respuesta.message);
        this.estado.set('ok');
      },
      error: () => {
        this.estado.set('error');
      },
    });
  }
}