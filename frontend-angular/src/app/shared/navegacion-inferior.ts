import { ChangeDetectionStrategy, Component } from '@angular/core';
import { RouterLink, RouterLinkActive } from '@angular/router';

/** Barra inferior de los mockups UI-2 y UI-4: Inicio, Contactos, Ayuda, Perfil. */
@Component({
  selector: 'app-navegacion-inferior',
  imports: [RouterLink, RouterLinkActive],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <nav class="barra" aria-label="Navegación principal">
      <a class="opcion" routerLink="/inicio" routerLinkActive="activa" ariaCurrentWhenActive="page">
        <span class="icono" aria-hidden="true">🏠</span>
        Inicio
      </a>
      <a
        class="opcion"
        routerLink="/contactos"
        routerLinkActive="activa"
        ariaCurrentWhenActive="page"
      >
        <span class="icono" aria-hidden="true">👥</span>
        Contactos
      </a>
      <a class="opcion" routerLink="/ayuda" routerLinkActive="activa" ariaCurrentWhenActive="page">
        <span class="icono" aria-hidden="true">❓</span>
        Ayuda
      </a>
      <button type="button" class="opcion" disabled>
        <span class="icono" aria-hidden="true">👤</span>
        Perfil
      </button>
    </nav>
  `,
  styles: `
    :host {
      position: fixed;
      right: 0;
      bottom: 0;
      left: 0;
      background: var(--color-surface);
      border-top: 1px solid #d5d9e3;
      box-shadow: 0 -2px 8px rgba(1, 32, 79, 0.08);
    }
    .barra {
      display: flex;
      max-width: 28rem;
      margin: 0 auto;
    }
    .opcion {
      flex: 1;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 0.25rem;
      min-height: 4rem;
      padding: 0.5rem 0.25rem;
      border: 0;
      background: transparent;
      color: #4b5563;
      font: inherit;
      font-size: 0.875rem;
      text-decoration: none;
      cursor: pointer;
    }
    .icono {
      font-size: 1.5rem;
      line-height: 1;
    }
    .opcion.activa {
      color: var(--color-primary);
      font-weight: 700;
    }
    .opcion:disabled {
      opacity: 0.5;
      cursor: not-allowed;
    }
  `,
})
export class NavegacionInferior { }