import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { RouterLink } from '@angular/router';

import { Auth } from '../../core/auth';
import { NavegacionInferior } from '../../shared/navegacion-inferior';

@Component({
  selector: 'app-panel-principal',
  imports: [RouterLink, NavegacionInferior],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="cabecera"></div>

    <main class="panel">
      <h1>Hola, {{ nombre() }} <span aria-hidden="true">👋</span></h1>
      <p class="pregunta">¿Qué deseas hacer?</p>

      <div class="rejilla">
        <a class="ficha" routerLink="/contactos">
          <span class="icono" aria-hidden="true">📞</span>
          Llamar
        </a>
        <a class="ficha" routerLink="/contactos">
          <span class="icono" aria-hidden="true">🎥</span>
          Videollamada
        </a>
        <a class="ficha" routerLink="/mensajes">
          <span class="icono" aria-hidden="true">💬</span>
          Mensajes
        </a>
        <a class="ficha" routerLink="/contactos">
          <span class="icono" aria-hidden="true">👥</span>
          Mis contactos
        </a>
        <a class="ficha ancha" routerLink="/recordatorios">
          <span class="icono" aria-hidden="true">⏰</span>
          Recordatorios
        </a>
        <a class="ficha ayuda" routerLink="/ayuda">
          <span class="icono" aria-hidden="true">🤝</span>
          Pedir ayuda
        </a>
      </div>

      <nav class="extras" aria-label="Más opciones">
        <a class="extra" routerLink="/mensajes-voz">Mensajes de voz</a>
        <a class="extra" routerLink="/historial">Historial de llamadas</a>
      </nav>

      <button type="button" class="salir" (click)="salir()">Cerrar sesión</button>
    </main>

    <app-navegacion-inferior />
  `,
  styles: `
    .cabecera {
      height: 3rem;
      background: var(--color-primary);
      border-radius: 0 0 var(--radius) var(--radius);
    }
    .panel {
      max-width: 28rem;
      margin: 0 auto;
      padding: var(--space-3) var(--space-3) 6rem;
    }
    h1 {
      margin-bottom: 0.25rem;
    }
    .pregunta {
      margin-top: 0;
      font-size: 1.375rem;
    }
    .rejilla {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: var(--space-2);
    }
    .ficha {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 0.5rem;
      min-height: 7rem;
      padding: var(--space-2);
      border: 0;
      border-radius: var(--radius);
      background: var(--color-surface);
      box-shadow: 0 2px 8px rgba(1, 32, 79, 0.1);
      color: var(--color-primary);
      font: inherit;
      font-weight: 700;
      text-align: center;
      text-decoration: none;
      cursor: pointer;
    }
    .icono {
      font-size: 2rem;
      line-height: 1;
    }
    .ancha {
      grid-column: 1 / -1;
    }
    .ayuda {
      grid-column: 1 / -1;
      background: var(--color-warning);
      color: #1f2937;
    }
    .extras {
      display: flex;
      flex-direction: column;
      gap: var(--space-1);
      margin-top: var(--space-2);
    }
    .extra {
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: var(--touch-target);
      color: var(--color-primary);
      font-weight: 700;
    }
    .salir {
      width: 100%;
      min-height: var(--touch-target);
      margin-top: var(--space-3);
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
    const completo = usuario?.display_name || usuario?.email || '';
    return completo.split(' ')[0];
  });

  protected salir(): void {
    this.auth.cerrarSesion();
  }
}