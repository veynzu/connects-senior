import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';

import { Auth } from '../../core/auth';

@Component({
    selector: 'app-panel-principal',
    changeDetection: ChangeDetectionStrategy.OnPush,
    template: `
    <main class="panel">
      <h1>Hola, {{ nombre() }}</h1>
      <p>Iniciaste sesión correctamente.</p>
      <p>Pantalla temporal: aquí irá el panel principal de contactos.</p>
      <button type="button" class="salir" (click)="salir()">Cerrar sesión</button>
    </main>
  `,
    styles: `
    .panel {
      max-width: 28rem;
      margin: 0 auto;
      padding: var(--space-3);
    }
    .salir {
      width: 100%;
      min-height: var(--touch-target);
      border: 2px solid var(--color-primary);
      border-radius: var(--radius);
      background: var(--color-surface);
      color: var(--color-primary);
      font: inherit;
      font-weight: 700;
      cursor: pointer;
    }
  `,
})
export class PanelPrincipal {
    private readonly auth = inject(Auth);

    protected readonly nombre = computed(() => {
        const usuario = this.auth.usuario();
        return usuario?.display_name || usuario?.email || '';
    });

    protected salir(): void {
        this.auth.cerrarSesion();
    }
}