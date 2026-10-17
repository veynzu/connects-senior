import { HttpErrorResponse } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { ActivatedRoute, RouterLink } from '@angular/router';

import { Auth } from '../../core/auth';
import {
    ETIQUETAS_ESTADO_RECORDATORIO,
    ETIQUETAS_REPETICION,
    ETIQUETAS_TIPO,
    Recordatorio,
    Recordatorios,
    fechaHoraLegible,
} from './recordatorios';

type Estado = 'cargando' | 'error' | 'listo';

@Component({
    selector: 'app-recordatorios-adulto',
    imports: [RouterLink],
    changeDetection: ChangeDetectionStrategy.OnPush,
    styleUrl: '../../shared/formulario.css',
    template: `
    <main class="pagina">
      <h1>Recordatorios</h1>

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
            <p>Todavía no hay recordatorios.</p>
          } @else {
            <ul class="lista">
              @for (r of lista(); track r.id) {
                <li class="tarjeta">
                  <p class="nombre">{{ tipo(r) }}</p>
                  <p>{{ r.description }}</p>
                  <p class="relacion">{{ fechaHora(r) }} · {{ repeticion(r) }}</p>
                  <p class="etiqueta">{{ estadoTexto(r) }}</p>

                  @if (confirmando() === r.id) {
                    <p role="alert">¿Seguro que quieres eliminar este recordatorio?</p>
                    <button
                      type="button"
                      class="entrar"
                      [disabled]="eliminando()"
                      (click)="eliminar(r.id)"
                    >
                      {{ eliminando() ? 'Eliminando…' : 'Sí, eliminar' }}
                    </button>
                    <button
                      type="button"
                      class="entrar secundario"
                      [disabled]="eliminando()"
                      (click)="confirmando.set('')"
                    >
                      No, conservar
                    </button>
                  } @else {
                    <a
                      class="enlace"
                      [routerLink]="['/adultos', adultoId, 'recordatorios', r.id, 'editar']"
                    >
                      Editar
                    </a>
                    <button
                      type="button"
                      class="entrar secundario"
                      (click)="confirmando.set(r.id)"
                    >
                      Eliminar
                    </button>
                  }
                </li>
              }
            </ul>
          }
          @if (errorEliminar()) {
            <p class="error-general" role="alert">{{ errorEliminar() }}</p>
          }
        }
      }

      <a class="entrar" [routerLink]="['/adultos', adultoId, 'recordatorios', 'nuevo']">
        Agregar recordatorio
      </a>
      <a class="enlace" routerLink="/familiar">Volver</a>
    </main>
  `,
})
export class RecordatoriosAdulto {
    private readonly ruta = inject(ActivatedRoute);
    private readonly servicio = inject(Recordatorios);
    private readonly auth = inject(Auth);

    protected readonly adultoId = this.ruta.snapshot.paramMap.get('adultoId') ?? '';

    protected readonly estado = signal<Estado>('cargando');
    protected readonly lista = signal<Recordatorio[]>([]);
    protected readonly mensajeError = signal('');
    protected readonly confirmando = signal('');
    protected readonly eliminando = signal(false);
    protected readonly errorEliminar = signal('');

    constructor() {
        this.cargar();
    }

    protected cargar(): void {
        this.estado.set('cargando');
        this.servicio.listar(this.adultoId).subscribe({
            next: (datos) => {
                this.lista.set(
                    [...datos].sort(
                        (a, b) => new Date(a.scheduled_at).getTime() - new Date(b.scheduled_at).getTime(),
                    ),
                );
                this.estado.set('listo');
            },
            error: (error: HttpErrorResponse) => {
                if (error.status === 401) {
                    this.auth.cerrarSesion();
                    return;
                }
                this.mensajeError.set(
                    error.status === 403
                        ? 'No tienes permiso para ver los recordatorios de este adulto mayor.'
                        : 'No pudimos cargar los recordatorios. Revisa tu conexión e inténtalo de nuevo.',
                );
                this.estado.set('error');
            },
        });
    }

    protected eliminar(id: string): void {
        this.eliminando.set(true);
        this.errorEliminar.set('');
        this.servicio.eliminar(id).subscribe({
            next: () => {
                this.lista.update((actual) => actual.filter((r) => r.id !== id));
                this.confirmando.set('');
                this.eliminando.set(false);
            },
            error: (error: HttpErrorResponse) => {
                this.eliminando.set(false);
                this.confirmando.set('');
                if (error.status === 401) {
                    this.auth.cerrarSesion();
                    return;
                }
                this.errorEliminar.set(
                    error.status === 403
                        ? 'No tienes permiso para eliminar este recordatorio.'
                        : 'No pudimos eliminar el recordatorio. Inténtalo de nuevo.',
                );
            },
        });
    }

    protected tipo(r: Recordatorio): string {
        return ETIQUETAS_TIPO[r.kind] ?? r.kind;
    }

    protected repeticion(r: Recordatorio): string {
        return ETIQUETAS_REPETICION[r.repeat] ?? r.repeat;
    }

    protected estadoTexto(r: Recordatorio): string {
        return ETIQUETAS_ESTADO_RECORDATORIO[r.status] ?? r.status;
    }

    protected fechaHora(r: Recordatorio): string {
        return fechaHoraLegible(r.scheduled_at);
    }
}