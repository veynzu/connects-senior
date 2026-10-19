import { HttpErrorResponse } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';

import { Auth } from '../../core/auth';
import { NavegacionInferior } from '../../shared/navegacion-inferior';
import { Contacto, Contactos } from './contactos';

type Estado = 'cargando' | 'error' | 'listo';

@Component({
  selector: 'app-mis-contactos',
  imports: [RouterLink, NavegacionInferior],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <main class="pagina">
      <h1>Mis contactos</h1>

      @switch (estado()) {
        @case ('cargando') {
          <p role="status">Cargando tus contactos…</p>
        }
        @case ('error') {
          <div class="error" role="alert">
            <p>{{ mensajeError() }}</p>
            <button type="button" class="reintentar" (click)="cargar()">Intentar de nuevo</button>
          </div>
        }
        @case ('listo') {
          @if (contactos().length === 0) {
            <p>Todavía no tienes contactos. Pídele a tu familiar que los agregue.</p>
          } @else {
            <ul class="lista">
              @for (c of contactos(); track c.id) {
                <li class="tarjeta">
                  <div class="persona">
                    @if (c.photo_url) {
                      <img class="foto" [src]="c.photo_url" alt="" />
                    } @else {
                      <span class="foto iniciales" aria-hidden="true">{{ iniciales(c) }}</span>
                    }
                    <div>
                      <p class="nombre">{{ c.first_name }} {{ c.last_name }}</p>
                      <p class="relacion">{{ c.relationship }}</p>
                    </div>
                  </div>

                  <div class="acciones">
                    @if (c.permissions.calls) {
                      <a
                        class="boton llamar"
                        [routerLink]="['/llamada', c.id]"
                        [queryParams]="{ audio: 1 }"
                        [attr.aria-label]="'Llamar a ' + c.first_name"
                      >
                        <span aria-hidden="true">📞</span> Llamar
                      </a>
                    }
                    @if (c.permissions.video_calls) {
                      <a
                        class="boton video"
                        [routerLink]="['/llamada', c.id]"
                        [attr.aria-label]="'Videollamada con ' + c.first_name"
                      >
                        <span aria-hidden="true">🎥</span> Videollamada
                      </a>
                    }
                  </div>
                </li>
              }
            </ul>
          }
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
    .lista {
      display: grid;
      gap: var(--space-2);
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .tarjeta {
      padding: var(--space-2);
      border-radius: var(--radius);
      background: var(--color-surface);
      box-shadow: 0 2px 8px rgba(1, 32, 79, 0.1);
    }
    .persona {
      display: flex;
      align-items: center;
      gap: var(--space-2);
      margin-bottom: var(--space-2);
    }
    .foto {
      flex: none;
      width: 4.5rem;
      height: 4.5rem;
      border-radius: 50%;
      object-fit: cover;
    }
    .iniciales {
      display: grid;
      place-items: center;
      background: #dfe6f7;
      color: var(--color-heading);
      font-size: 1.5rem;
      font-weight: 700;
    }
    .nombre {
      margin: 0;
      color: var(--color-heading);
      font-size: 1.25rem;
      font-weight: 700;
    }
    .relacion {
      margin: 0;
      font-size: 1rem;
    }
    .acciones {
      display: flex;
      gap: var(--space-1);
    }
    .boton {
      flex: 1;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 0.5rem;
      min-height: var(--touch-target);
      border: 0;
      border-radius: var(--radius);
      color: #fff;
      font: inherit;
      font-weight: 700;
      text-decoration: none;
      cursor: pointer;
    }
    .llamar {
      background: var(--color-primary);
    }
    .video {
      background: var(--color-secondary);
    }
    .error {
      padding: var(--space-2);
      border: 2px solid #b3141b;
      border-radius: var(--radius);
      background: #fff0f0;
      color: #7a0c11;
      font-weight: 700;
    }
    .reintentar {
      min-height: var(--touch-target);
      padding: 0 var(--space-3);
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
export class MisContactos {
  private readonly servicio = inject(Contactos);
  private readonly auth = inject(Auth);

  protected readonly estado = signal<Estado>('cargando');
  protected readonly contactos = signal<Contacto[]>([]);
  protected readonly mensajeError = signal('');

  constructor() {
    this.cargar();
  }

  protected cargar(): void {
    this.estado.set('cargando');
    this.servicio.listar().subscribe({
      next: (lista) => {
        this.contactos.set(lista);
        this.estado.set('listo');
      },
      error: (error: HttpErrorResponse) => {
        if (error.status === 401) {
          this.auth.cerrarSesion();
          return;
        }
        this.mensajeError.set(this.traducirError(error.status));
        this.estado.set('error');
      },
    });
  }

  protected iniciales(c: Contacto): string {
    return `${c.first_name.charAt(0)}${c.last_name.charAt(0)}`.toUpperCase();
  }

  private traducirError(estado: number): string {
    switch (estado) {
      case 403:
        return 'No tienes permiso para ver estos contactos.';
      case 503:
        return 'El servicio no está disponible ahora. Inténtalo más tarde.';
      default:
        return 'No pudimos cargar tus contactos. Revisa tu conexión e inténtalo de nuevo.';
    }
  }
}