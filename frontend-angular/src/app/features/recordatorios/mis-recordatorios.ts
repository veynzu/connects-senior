import { HttpErrorResponse } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { Observable } from 'rxjs';

import { Auth } from '../../core/auth';
import {
    ETIQUETAS_ESTADO_RECORDATORIO,
    ETIQUETAS_TIPO,
    Recordatorio,
    Recordatorios,
    fechaHoraLegible,
} from './recordatorios';

type Estado = 'cargando' | 'error' | 'listo';

@Component({
    selector: 'app-mis-recordatorios',
    imports: [RouterLink],
    changeDetection: ChangeDetectionStrategy.OnPush,
    styleUrl: '../../shared/formulario.css',
    template: `
    <main class="pagina">
      <h1>Mis recordatorios</h1>

      @switch (estado()) {
        @case ('cargando') {
          <p role="status">Cargando recordatorios…</p>
        }
        @case ('error') {
          <p class="error-general" role="alert">{{ mensajeError() }}</p>
          <button type="button" class="entrar secundario" (click)="cargar()">
            Intentar de nuevo
          </button>
        }
        @case ('listo') {
          @if (lista().length === 0) {
            <p>No tienes recordatorios por ahora.</p>
          } @else {
            <ul class="lista">
              @for (r of lista(); track r.id) {
                <li class="tarjeta">
                  <p class="nombre">{{ tipo(r) }}</p>
                  <p>{{ r.description }}</p>
                  <p class="relacion">{{ fechaHora(r) }}</p>
                  <p class="etiqueta">{{ estadoTexto(r) }}</p>

                  @if (r.status === 'pendiente') {
                    <button
                      type="button"
                      class="entrar"
                      [disabled]="ocupado() === r.id"
                      (click)="accion(r.id, 'confirmar')"
                    >
                      Ya lo hice
                    </button>
                    <button
                      type="button"
                      class="entrar secundario"
                      [disabled]="ocupado() === r.id"
                      (click)="accion(r.id, 'posponer')"
                    >
                      Recordarme en 10 minutos
                    </button>
                  }
                </li>
              }
            </ul>
          }
          @if (errorAccion()) {
            <p class="error-general" role="alert">{{ errorAccion() }}</p>
          }
        }
      }

      <a class="enlace" routerLink="/inicio">Volver</a>
    </main>
  `,
})
export class MisRecordatorios {
    private readonly servicio = inject(Recordatorios);
    private readonly auth = inject(Auth);

    protected readonly estado = signal<Estado>('cargando');
    protected readonly lista = signal<Recordatorio[]>([]);
    protected readonly mensajeError = signal('');
    protected readonly ocupado = signal('');
    protected readonly errorAccion = signal('');

    constructor() {
        this.cargar();
    }

    protected cargar(): void {
        this.estado.set('cargando');
        this.servicio.listar().subscribe({
            next: (datos) => {
                this.lista.set(this.ordenar(datos));
                this.estado.set('listo');
            },
            error: (error: HttpErrorResponse) => {
                if (error.status === 401) {
                    this.auth.cerrarSesion();
                    return;
                }
                this.mensajeError.set(
                    'No pudimos cargar tus recordatorios. Revisa tu conexión e inténtalo de nuevo.',
                );
                this.estado.set('error');
            },
        });
    }

    protected accion(id: string, tipo: 'confirmar' | 'posponer'): void {
        const peticion: Observable<Recordatorio> =
            tipo === 'confirmar' ? this.servicio.confirmar(id) : this.servicio.posponer(id);
        this.ocupado.set(id);
        this.errorAccion.set('');
        peticion.subscribe({
            next: (actualizado) => {
                this.lista.update((actual) =>
                    this.ordenar(actual.map((r) => (r.id === id ? actualizado : r))),
                );
                this.ocupado.set('');
            },
            error: (error: HttpErrorResponse) => {
                this.ocupado.set('');
                if (error.status === 401) {
                    this.auth.cerrarSesion();
                    return;
                }
                this.errorAccion.set('No pudimos guardar tu respuesta. Inténtalo de nuevo.');
            },
        });
    }

    protected tipo(r: Recordatorio): string {
        return ETIQUETAS_TIPO[r.kind] ?? r.kind;
    }

    protected estadoTexto(r: Recordatorio): string {
        return ETIQUETAS_ESTADO_RECORDATORIO[r.status] ?? r.status;
    }

    protected fechaHora(r: Recordatorio): string {
        return fechaHoraLegible(r.scheduled_at);
    }

    private ordenar(datos: Recordatorio[]): Recordatorio[] {
        return [...datos].sort(
            (a, b) => new Date(a.scheduled_at).getTime() - new Date(b.scheduled_at).getTime(),
        );
    }
}