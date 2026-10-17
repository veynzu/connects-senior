import { HttpErrorResponse } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { ActivatedRoute, RouterLink } from '@angular/router';

import { Auth } from '../../core/auth';
import { Contacto, Contactos } from './contactos';

type Estado = 'cargando' | 'error' | 'listo';

@Component({
    selector: 'app-contactos-adulto',
    imports: [RouterLink],
    changeDetection: ChangeDetectionStrategy.OnPush,
    styleUrl: '../../shared/formulario.css',
    template: `
    <main class="pagina">
      <h1>Contactos</h1>

      @switch (estado()) {
        @case ('cargando') {
          <p role="status">Cargando contactos…</p>
        }
        @case ('error') {
          <p class="error-general" role="alert">{{ mensajeError() }}</p>
          <button type="button" class="entrar secundario" (click)="cargar()">
            Intentar de nuevo
          </button>
        }
        @case ('listo') {
          @if (lista().length === 0) {
            <p>Este adulto mayor todavía no tiene contactos.</p>
          } @else {
            <ul class="lista">
              @for (c of lista(); track c.id) {
                <li>
                  <a
                    class="tarjeta tarjeta-enlace"
                    [routerLink]="['/adultos', adultoId, 'contactos', c.id]"
                  >
                    <span class="persona">
                      <span class="foto" aria-hidden="true">{{ iniciales(c) }}</span>
                      <span>
                        <span class="nombre">{{ c.first_name }} {{ c.last_name }}</span>
                        <br />
                        <span class="relacion">{{ c.relationship }}</span>
                        @if (c.permissions.help_alerts) {
                          <br />
                          <span class="etiqueta">Contacto de confianza</span>
                        }
                      </span>
                    </span>
                  </a>
                </li>
              }
            </ul>
          }
        }
      }

      <a class="entrar" [routerLink]="['/adultos', adultoId, 'contactos', 'nuevo']">
        Agregar contacto
      </a>
      <a class="enlace" routerLink="/familiar">Volver</a>
    </main>
  `,
})
export class ContactosAdulto {
    private readonly ruta = inject(ActivatedRoute);
    private readonly servicio = inject(Contactos);
    private readonly auth = inject(Auth);

    protected readonly adultoId = this.ruta.snapshot.paramMap.get('adultoId') ?? '';

    protected readonly estado = signal<Estado>('cargando');
    protected readonly lista = signal<Contacto[]>([]);
    protected readonly mensajeError = signal('');

    constructor() {
        this.cargar();
    }

    protected cargar(): void {
        this.estado.set('cargando');
        this.servicio.listar(this.adultoId).subscribe({
            next: (contactos) => {
                this.lista.set(contactos);
                this.estado.set('listo');
            },
            error: (error: HttpErrorResponse) => {
                if (error.status === 401) {
                    this.auth.cerrarSesion();
                    return;
                }
                this.mensajeError.set(
                    error.status === 403
                        ? 'No tienes permiso para ver los contactos de este adulto mayor.'
                        : 'No pudimos cargar los contactos. Revisa tu conexión e inténtalo de nuevo.',
                );
                this.estado.set('error');
            },
        });
    }

    protected iniciales(c: Contacto): string {
        return `${c.first_name.charAt(0)}${c.last_name.charAt(0)}`.toUpperCase();
    }
}