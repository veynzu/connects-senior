import { HttpErrorResponse } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';

import { Auth } from '../../core/auth';
import { ContactoMensaje, Mensajes } from './mensajes';

type Estado = 'cargando' | 'error' | 'listo';

@Component({
    selector: 'app-lista-mensajes',
    imports: [RouterLink],
    changeDetection: ChangeDetectionStrategy.OnPush,
    styleUrl: '../../shared/formulario.css',
    template: `
    <main class="pagina">
      <h1>Mensajes</h1>
      <p>¿A quién quieres escribirle?</p>

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
            <p>No tienes contactos con quienes enviar mensajes.</p>
          } @else {
            <ul class="lista">
              @for (c of lista(); track c.id) {
                <li>
                  <a class="tarjeta tarjeta-enlace" [routerLink]="['/mensajes', c.id]">
                    <span class="nombre">{{ c.first_name }} {{ c.last_name }}</span>
                    <br />
                    <span class="relacion">{{ c.relationship }}</span>
                  </a>
                </li>
              }
            </ul>
          }
        }
      }

      <a class="enlace" routerLink="/inicio">Volver</a>
    </main>
  `,
})
export class ListaMensajes {
    private readonly servicio = inject(Mensajes);
    private readonly auth = inject(Auth);

    protected readonly estado = signal<Estado>('cargando');
    protected readonly lista = signal<ContactoMensaje[]>([]);
    protected readonly mensajeError = signal('');

    constructor() {
        this.cargar();
    }

    protected cargar(): void {
        this.estado.set('cargando');
        this.servicio.contactos().subscribe({
            next: (contactos) => {
                this.lista.set(contactos.filter((c) => c.permissions?.messages));
                this.estado.set('listo');
            },
            error: (error: HttpErrorResponse) => {
                if (error.status === 401) {
                    this.auth.cerrarSesion();
                    return;
                }
                this.mensajeError.set(
                    'No pudimos cargar tus contactos. Revisa tu conexión e inténtalo de nuevo.',
                );
                this.estado.set('error');
            },
        });
    }
}