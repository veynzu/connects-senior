import { HttpErrorResponse } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { Observable } from 'rxjs';

import { Auth } from '../../core/auth';
import {
    Alertas,
    ETIQUETAS_CATEGORIA,
    ETIQUETAS_ESTADO,
    EstadoSolicitud,
    SolicitudDetalle,
    fechaLegible,
    horaLegible,
} from './alertas';

type Estado = 'cargando' | 'error' | 'listo';

@Component({
    selector: 'app-alerta-recibida',
    imports: [RouterLink],
    changeDetection: ChangeDetectionStrategy.OnPush,
    styleUrl: '../../shared/formulario.css',
    template: `
    <main class="pagina">
      <h1>Alerta recibida</h1>

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
          @if (solicitud(); as a) {
            <section class="confirmacion" aria-labelledby="titulo-alerta">
              <h2 id="titulo-alerta">Solicitud de ayuda</h2>
              <p>{{ a.adult_name }} necesita ayuda</p>
              @if (tipo(a)) {
                <p>{{ tipo(a) }}</p>
              }
              <dl class="detalle">
                <div>
                  <dt>Fecha</dt>
                  <dd>{{ fecha(a) }}</dd>
                </div>
                <div>
                  <dt>Hora</dt>
                  <dd>{{ hora(a) }}</dd>
                </div>
                <div>
                  <dt>Estado</dt>
                  <dd>{{ estadoTexto(a) }}</dd>
                </div>
              </dl>
            </section>

            <h2>Ubicación</h2>
            @if (mapa(a); as enlace) {
              <p>El adulto mayor compartió su ubicación.</p>
              <a
                class="entrar secundario"
                [href]="enlace"
                target="_blank"
                rel="noopener noreferrer"
              >
                Ver ubicación
              </a>
            } @else {
              <p>El adulto mayor no compartió su ubicación.</p>
            }

            @if (a.status === 'pendiente') {
              <button type="button" class="entrar" [disabled]="guardando()" (click)="atender()">
                {{ guardando() ? 'Guardando…' : 'Atender solicitud' }}
              </button>
            }

            <fieldset class="permisos">
              <legend>Actualiza el estado de la solicitud</legend>
              @for (op of estados; track op.valor) {
                <label class="permiso">
                  <input
                    type="radio"
                    name="estado"
                    [value]="op.valor"
                    [checked]="a.status === op.valor"
                    [disabled]="guardando()"
                    (change)="cambiar(op.valor)"
                  />
                  {{ op.texto }}
                </label>
              }
            </fieldset>

            @if (errorCambio()) {
              <p class="error-general" role="alert">{{ errorCambio() }}</p>
            }
          }
        }
      }

      <a class="enlace" [routerLink]="['/adultos', adultoId, 'solicitudes']">Volver</a>
    </main>
  `,
})
export class AlertaRecibida {
    private readonly ruta = inject(ActivatedRoute);
    private readonly servicio = inject(Alertas);
    private readonly auth = inject(Auth);

    protected readonly adultoId = this.ruta.snapshot.paramMap.get('adultoId') ?? '';
    private readonly solicitudId = this.ruta.snapshot.paramMap.get('id') ?? '';

    protected readonly estados: readonly { valor: EstadoSolicitud; texto: string }[] = [
        { valor: 'pendiente', texto: ETIQUETAS_ESTADO.pendiente },
        { valor: 'en_atencion', texto: ETIQUETAS_ESTADO.en_atencion },
        { valor: 'atendida', texto: ETIQUETAS_ESTADO.atendida },
    ];

    protected readonly estado = signal<Estado>('cargando');
    protected readonly solicitud = signal<SolicitudDetalle | null>(null);
    protected readonly mensajeError = signal('');
    protected readonly guardando = signal(false);
    protected readonly errorCambio = signal('');

    constructor() {
        this.cargar();
    }

    protected cargar(): void {
        this.estado.set('cargando');
        this.servicio.obtener(this.solicitudId).subscribe({
            next: (datos) => {
                this.solicitud.set(datos);
                this.estado.set('listo');
            },
            error: (error: HttpErrorResponse) => {
                if (error.status === 401) {
                    this.auth.cerrarSesion();
                    return;
                }
                this.mensajeError.set(
                    error.status === 404
                        ? 'No encontramos esta solicitud.'
                        : error.status === 403
                            ? 'No tienes permiso para ver esta solicitud.'
                            : 'No pudimos cargar la solicitud. Revisa tu conexión e inténtalo de nuevo.',
                );
                this.estado.set('error');
            },
        });
    }

    protected atender(): void {
        this.guardar(this.servicio.atender(this.solicitudId));
    }

    protected cambiar(estado: EstadoSolicitud): void {
        if (this.solicitud()?.status === estado) {
            return;
        }
        this.guardar(this.servicio.cambiarEstado(this.solicitudId, estado));
    }

    protected tipo(a: SolicitudDetalle): string {
        return a.category ? (ETIQUETAS_CATEGORIA[a.category] ?? '') : '';
    }

    protected estadoTexto(a: SolicitudDetalle): string {
        return ETIQUETAS_ESTADO[a.status] ?? a.status;
    }

    protected fecha(a: SolicitudDetalle): string {
        return fechaLegible(a.created_at);
    }

    protected hora(a: SolicitudDetalle): string {
        return horaLegible(a.created_at);
    }

    /** Enlace a un mapa solo si el adulto mayor compartió su ubicación. */
    protected mapa(a: SolicitudDetalle): string | null {
        if (typeof a.latitude === 'number' && typeof a.longitude === 'number') {
            return `https://www.google.com/maps?q=${a.latitude},${a.longitude}`;
        }
        return null;
    }

    private guardar(peticion: Observable<SolicitudDetalle>): void {
        this.guardando.set(true);
        this.errorCambio.set('');
        peticion.subscribe({
            next: (actualizada) => {
                const actual = this.solicitud();
                this.solicitud.set(actual ? { ...actual, ...actualizada } : actualizada);
                this.guardando.set(false);
            },
            error: (error: HttpErrorResponse) => {
                this.guardando.set(false);
                if (error.status === 401) {
                    this.auth.cerrarSesion();
                    return;
                }
                this.errorCambio.set(
                    error.status === 403
                        ? 'No tienes permiso para cambiar el estado de esta solicitud.'
                        : 'No pudimos actualizar el estado. Inténtalo de nuevo.',
                );
            },
        });
    }
}