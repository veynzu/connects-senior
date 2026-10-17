import { HttpErrorResponse } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';

import { Auth } from '../../core/auth';
import { Contacto, Contactos } from './contactos';

type Estado = 'cargando' | 'error' | 'listo';

@Component({
    selector: 'app-detalle-contacto',
    imports: [RouterLink],
    changeDetection: ChangeDetectionStrategy.OnPush,
    styleUrl: '../../shared/formulario.css',
    template: `
    <main class="pagina">
      @switch (estado()) {
        @case ('cargando') {
          <p role="status">Cargando…</p>
        }
        @case ('error') {
          <p class="error-general" role="alert">{{ mensajeError() }}</p>
          <button type="button" class="entrar secundario" (click)="cargar()">
            Intentar de nuevo
          </button>
        }
        @case ('listo') {
          @if (contacto(); as c) {
            <div class="persona">
              <span class="foto" aria-hidden="true">{{ iniciales(c) }}</span>
              <div>
                <h1 class="nombre">{{ c.first_name }} {{ c.last_name }}</h1>
                <p class="relacion">{{ c.relationship }}</p>
                @if (c.permissions.help_alerts) {
                  <span class="etiqueta">Contacto de confianza</span>
                }
              </div>
            </div>

            <dl class="detalle">
              @if (c.phone) {
                <div>
                  <dt>Teléfono</dt>
                  <dd>{{ c.phone }}</dd>
                </div>
              }
              @if (c.email) {
                <div>
                  <dt>Correo</dt>
                  <dd>{{ c.email }}</dd>
                </div>
              }
            </dl>

            <h2>Permisos</h2>
            <dl class="detalle">
              <div>
                <dt>Llamadas</dt>
                <dd>{{ c.permissions.calls ? 'Sí' : 'No' }}</dd>
              </div>
              <div>
                <dt>Videollamadas</dt>
                <dd>{{ c.permissions.video_calls ? 'Sí' : 'No' }}</dd>
              </div>
              <div>
                <dt>Mensajes</dt>
                <dd>{{ c.permissions.messages ? 'Sí' : 'No' }}</dd>
              </div>
              <div>
                <dt>Alertas de ayuda</dt>
                <dd>{{ c.permissions.help_alerts ? 'Sí' : 'No' }}</dd>
              </div>
            </dl>

            <a class="entrar secundario" [routerLink]="[...rutaLista, c.id, 'editar']">
              Editar contacto
            </a>
            <a class="entrar secundario" [routerLink]="[...rutaLista, c.id, 'editar']">
              Modificar permisos
            </a>

            @if (confirmando()) {
              <div class="confirmacion" role="alert">
                <p>
                  ¿Seguro que quieres eliminar a {{ c.first_name }}? Esta acción no se puede
                  deshacer.
                </p>
                @if (errorEliminar()) {
                  <p>{{ errorEliminar() }}</p>
                }
                <button
                  type="button"
                  class="entrar peligro"
                  [disabled]="eliminando()"
                  (click)="eliminar()"
                >
                  {{ eliminando() ? 'Eliminando…' : 'Sí, eliminar' }}
                </button>
                <button
                  type="button"
                  class="entrar secundario"
                  [disabled]="eliminando()"
                  (click)="confirmando.set(false)"
                >
                  Cancelar
                </button>
              </div>
            } @else {
              <button type="button" class="entrar peligro" (click)="confirmando.set(true)">
                Eliminar contacto
              </button>
            }
          }
        }
      }

      <a class="enlace" [routerLink]="rutaLista">Volver a los contactos</a>
    </main>
  `,
})
export class DetalleContacto {
    private readonly ruta = inject(ActivatedRoute);
    private readonly router = inject(Router);
    private readonly servicio = inject(Contactos);
    private readonly auth = inject(Auth);

    private readonly adultoId = this.ruta.snapshot.paramMap.get('adultoId') ?? '';
    private readonly contactoId = this.ruta.snapshot.paramMap.get('id') ?? '';
    protected readonly rutaLista = ['/adultos', this.adultoId, 'contactos'];

    protected readonly estado = signal<Estado>('cargando');
    protected readonly contacto = signal<Contacto | null>(null);
    protected readonly mensajeError = signal('');
    protected readonly confirmando = signal(false);
    protected readonly eliminando = signal(false);
    protected readonly errorEliminar = signal('');

    constructor() {
        this.cargar();
    }

    protected cargar(): void {
        this.estado.set('cargando');
        this.servicio.listar(this.adultoId).subscribe({
            next: (lista) => {
                const encontrado = lista.find((c) => c.id === this.contactoId);
                if (!encontrado) {
                    this.mensajeError.set('No encontramos este contacto.');
                    this.estado.set('error');
                    return;
                }
                this.contacto.set(encontrado);
                this.estado.set('listo');
            },
            error: (error: HttpErrorResponse) => {
                if (error.status === 401) {
                    this.auth.cerrarSesion();
                    return;
                }
                this.mensajeError.set(
                    error.status === 403
                        ? 'No tienes permiso para ver este contacto.'
                        : 'No pudimos cargar el contacto. Revisa tu conexión e inténtalo de nuevo.',
                );
                this.estado.set('error');
            },
        });
    }

    protected eliminar(): void {
        this.eliminando.set(true);
        this.errorEliminar.set('');
        this.servicio.eliminar(this.contactoId).subscribe({
            next: () => {
                this.eliminando.set(false);
                this.router.navigate(this.rutaLista);
            },
            error: (error: HttpErrorResponse) => {
                this.eliminando.set(false);
                if (error.status === 401) {
                    this.auth.cerrarSesion();
                    return;
                }
                this.errorEliminar.set(
                    error.status === 403
                        ? 'No tienes permiso para eliminar este contacto.'
                        : 'No pudimos eliminar el contacto. Inténtalo de nuevo.',
                );
            },
        });
    }

    protected iniciales(c: Contacto): string {
        return `${c.first_name.charAt(0)}${c.last_name.charAt(0)}`.toUpperCase();
    }
}