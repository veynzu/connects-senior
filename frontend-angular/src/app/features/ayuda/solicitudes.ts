import { HttpErrorResponse } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { ActivatedRoute, RouterLink } from '@angular/router';

import { Auth } from '../../core/auth';
import {
    Alertas,
    ETIQUETAS_CATEGORIA,
    ETIQUETAS_ESTADO,
    Solicitud,
    fechaLegible,
    horaLegible,
} from './alertas';

type Estado = 'cargando' | 'error' | 'listo';

@Component({
    selector: 'app-solicitudes',
    imports: [RouterLink],
    changeDetection: ChangeDetectionStrategy.OnPush,
    styleUrl: '../../shared/formulario.css',
    template: `
    <main class="pagina">
      <h1>Solicitudes de ayuda</h1>

      @switch (estado()) {
        @case ('cargando') {
          <p role="status">Cargando solicitudes…</p>
        }
        @case ('error') {
          <p class="error-general" role="alert">{{ mensajeError() }}</p>
          <button type="button" class="entrar secundario" (click)="cargar()">
            Intentar de nuevo
          </button>
        }
        @case ('listo') {
          @if (lista().length === 0) {
            <p>Todavía no hay solicitudes de ayuda.</p>
          } @else {
            <ul class="lista">
              @for (s of lista(); track s.id) {
                <li>
                  <a
                    class="tarjeta tarjeta-enlace"
                    [routerLink]="['/adultos', adultoId, 'solicitudes', s.id]"
                  >
                    <span class="nombre">{{ titulo(s) }}</span>
                    <br />
                    <span class="relacion">{{ fecha(s) }} · {{ hora(s) }}</span>
                    <br />
                    <span class="etiqueta">{{ estadoTexto(s) }}</span>
                  </a>
                </li>
              }
            </ul>
          }
        }
      }

      <a class="enlace" routerLink="/familiar">Volver</a>
    </main>
  `,
})
export class Solicitudes {
    private readonly ruta = inject(ActivatedRoute);
    private readonly servicio = inject(Alertas);
    private readonly auth = inject(Auth);

    protected readonly adultoId = this.ruta.snapshot.paramMap.get('adultoId') ?? '';

    protected readonly estado = signal<Estado>('cargando');
    protected readonly lista = signal<Solicitud[]>([]);
    protected readonly mensajeError = signal('');

    constructor() {
        this.cargar();
    }

    protected cargar(): void {
        this.estado.set('cargando');
        this.servicio.listar(this.adultoId).subscribe({
            next: (solicitudes) => {
                const ordenadas = [...solicitudes].sort(
                    (a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
                );
                this.lista.set(ordenadas);
                this.estado.set('listo');
            },
            error: (error: HttpErrorResponse) => {
                if (error.status === 401) {
                    this.auth.cerrarSesion();
                    return;
                }
                this.mensajeError.set(
                    error.status === 403
                        ? 'No tienes permiso para ver las solicitudes de este adulto mayor.'
                        : 'No pudimos cargar las solicitudes. Revisa tu conexión e inténtalo de nuevo.',
                );
                this.estado.set('error');
            },
        });
    }

    protected titulo(s: Solicitud): string {
        return (s.category && ETIQUETAS_CATEGORIA[s.category]) ?? 'Solicitud de ayuda';
    }

    protected estadoTexto(s: Solicitud): string {
        return ETIQUETAS_ESTADO[s.status] ?? s.status;
    }

    protected fecha(s: Solicitud): string {
        return fechaLegible(s.created_at);
    }

    protected hora(s: Solicitud): string {
        return horaLegible(s.created_at);
    }
}