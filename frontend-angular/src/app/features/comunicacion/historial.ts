import { HttpClient, HttpErrorResponse, HttpParams } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, Injectable, inject, signal } from '@angular/core';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { Observable, catchError, forkJoin, of } from 'rxjs';

import { API_BASE_URL } from '../../core/api-config';
import { Auth } from '../../core/auth';

/*
 * Contrato v1.0, L3: GET /api/calls (adult_id solo si quien llama es familiar o cuidador)
 * -> { id, contact_id, started_at, status }.
 */
export type EstadoLlamada = 'completada' | 'no_contestada' | 'cancelada';

export interface LlamadaHistorial {
    id: string;
    contact_id: string;
    started_at: string;
    status: EstadoLlamada;
}

interface ContactoNombre {
    id: string;
    first_name: string;
    last_name: string;
}

const ETIQUETAS_LLAMADA: Record<EstadoLlamada, string> = {
    completada: 'Completada',
    no_contestada: 'No contestada',
    cancelada: 'Cancelada',
};

@Injectable({ providedIn: 'root' })
export class HistorialLlamadas {
    private readonly http = inject(HttpClient);

    private parametros(adultId: string): HttpParams {
        return adultId ? new HttpParams().set('adult_id', adultId) : new HttpParams();
    }

    llamadas(adultId: string): Observable<LlamadaHistorial[]> {
        return this.http.get<LlamadaHistorial[]>(`${API_BASE_URL}/api/calls`, {
            params: this.parametros(adultId),
        });
    }

    contactos(adultId: string): Observable<ContactoNombre[]> {
        return this.http.get<ContactoNombre[]>(`${API_BASE_URL}/api/contacts`, {
            params: this.parametros(adultId),
        });
    }
}

type Estado = 'cargando' | 'error' | 'listo';

@Component({
    selector: 'app-historial',
    imports: [RouterLink],
    changeDetection: ChangeDetectionStrategy.OnPush,
    styleUrl: '../../shared/formulario.css',
    template: `
    <main class="pagina">
      <h1>Historial de llamadas</h1>

      @switch (estado()) {
        @case ('cargando') {
          <p role="status">Cargando historial…</p>
        }
        @case ('error') {
          <p class="error-general" role="alert">{{ mensajeError() }}</p>
          <button type="button" class="entrar secundario" (click)="cargar()">
            Intentar de nuevo
          </button>
        }
        @case ('listo') {
          @if (lista().length === 0) {
            <p>Todavía no hay llamadas.</p>
          } @else {
            <ul class="lista">
              @for (l of lista(); track l.id) {
                <li class="tarjeta">
                  <p class="nombre">{{ nombre(l) }}</p>
                  <p class="relacion">{{ fechaHora(l) }}</p>
                  <p class="etiqueta">{{ estadoTexto(l) }}</p>
                </li>
              }
            </ul>
          }
        }
      }

      <a class="enlace" [routerLink]="rutaVolver">Volver</a>
    </main>
  `,
})
export class Historial {
    private readonly ruta = inject(ActivatedRoute);
    private readonly servicio = inject(HistorialLlamadas);
    private readonly auth = inject(Auth);

    /** Solo existe cuando lo abre un familiar o cuidador. */
    private readonly adultoId = this.ruta.snapshot.paramMap.get('adultoId') ?? '';
    protected readonly rutaVolver = this.adultoId ? '/familiar' : '/inicio';

    protected readonly estado = signal<Estado>('cargando');
    protected readonly lista = signal<LlamadaHistorial[]>([]);
    protected readonly mensajeError = signal('');

    private nombres = new Map<string, string>();

    constructor() {
        this.cargar();
    }

    protected cargar(): void {
        this.estado.set('cargando');
        forkJoin({
            llamadas: this.servicio.llamadas(this.adultoId),
            contactos: this.servicio
                .contactos(this.adultoId)
                .pipe(catchError(() => of([] as ContactoNombre[]))),
        }).subscribe({
            next: ({ llamadas, contactos }) => {
                this.nombres = new Map(contactos.map((c) => [c.id, `${c.first_name} ${c.last_name}`]));
                this.lista.set(
                    [...llamadas].sort(
                        (a, b) => new Date(b.started_at).getTime() - new Date(a.started_at).getTime(),
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
                        ? 'No tienes permiso para ver este historial.'
                        : 'No pudimos cargar el historial. Revisa tu conexión e inténtalo de nuevo.',
                );
                this.estado.set('error');
            },
        });
    }

    protected nombre(l: LlamadaHistorial): string {
        return this.nombres.get(l.contact_id) ?? 'Contacto';
    }

    protected estadoTexto(l: LlamadaHistorial): string {
        return ETIQUETAS_LLAMADA[l.status] ?? l.status;
    }

    protected fechaHora(l: LlamadaHistorial): string {
        const f = new Date(l.started_at);
        return `${f.toLocaleDateString('es-CO')} ${f.toLocaleTimeString('es-CO', {
            hour: '2-digit',
            minute: '2-digit',
        })}`;
    }
}