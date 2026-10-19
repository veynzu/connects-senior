import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, Injectable, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { Observable } from 'rxjs';

import { API_BASE_URL } from '../../core/api-config';
import { Auth } from '../../core/auth';

/*
 * Contrato v1.0, V1 y V2 (RF-21).
 * V1: POST /api/calls/voice-messages (multipart: contact_id y audio/webm, máximo 5 MB).
 * V2: GET /api/calls/voice-messages -> { id, from_name, created_at, audio_url }.
 */
export interface MensajeVoz {
    id: string;
    from_name: string;
    created_at: string;
    audio_url: string;
}

export interface VozEnviada {
    id: string;
    created_at: string;
}

@Injectable({ providedIn: 'root' })
export class MensajesVoz {
    private readonly http = inject(HttpClient);
    private readonly url = `${API_BASE_URL}/api/calls/voice-messages`;

    /** V2 */
    listar(): Observable<MensajeVoz[]> {
        return this.http.get<MensajeVoz[]>(this.url);
    }

    /** V1: no se fija Content-Type; el navegador lo arma solo con el audio adjunto. */
    enviar(contactId: string, audio: Blob): Observable<VozEnviada> {
        const datos = new FormData();
        datos.append('contact_id', contactId);
        datos.append('audio', audio, 'mensaje.webm');
        return this.http.post<VozEnviada>(this.url, datos);
    }
}

type Estado = 'cargando' | 'error' | 'listo';

@Component({
    selector: 'app-buzon-voz',
    imports: [RouterLink],
    changeDetection: ChangeDetectionStrategy.OnPush,
    styleUrl: '../../shared/formulario.css',
    template: `
    <main class="pagina">
      <h1>Mensajes de voz</h1>

      @switch (estado()) {
        @case ('cargando') {
          <p role="status">Cargando mensajes de voz…</p>
        }
        @case ('error') {
          <p class="error-general" role="alert">{{ mensajeError() }}</p>
          <button type="button" class="entrar secundario" (click)="cargar()">
            Intentar de nuevo
          </button>
        }
        @case ('listo') {
          @if (lista().length === 0) {
            <p>No tienes mensajes de voz.</p>
          } @else {
            <ul class="lista">
              @for (m of lista(); track m.id) {
                <li class="tarjeta">
                  <p class="nombre">{{ m.from_name }}</p>
                  <p class="relacion">{{ fechaHora(m) }}</p>
                  @if (urlSegura(m.audio_url); as url) {
                    <audio controls preload="none" [src]="url">
                      Tu navegador no puede reproducir este audio.
                    </audio>
                  } @else {
                    <p class="error-general" role="alert">Este audio no está disponible.</p>
                  }
                </li>
              }
            </ul>
          }
          <p class="nota">Los audios se pueden escuchar durante unos minutos. Si no suenan, toca Actualizar.</p>
          <button type="button" class="entrar secundario" (click)="cargar()">Actualizar</button>
        }
      }

      <a class="enlace" [routerLink]="rutaVolver()">Volver</a>
    </main>
  `,
})
export class BuzonVoz {
    private readonly servicio = inject(MensajesVoz);
    private readonly auth = inject(Auth);

    protected readonly estado = signal<Estado>('cargando');
    protected readonly lista = signal<MensajeVoz[]>([]);
    protected readonly mensajeError = signal('');

    constructor() {
        this.cargar();
    }

    protected rutaVolver(): string {
        return this.auth.rutaInicial();
    }

    protected cargar(): void {
        this.estado.set('cargando');
        this.servicio.listar().subscribe({
            next: (mensajes) => {
                this.lista.set(
                    [...mensajes].sort(
                        (a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
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
                    'No pudimos cargar los mensajes de voz. Revisa tu conexión e inténtalo de nuevo.',
                );
                this.estado.set('error');
            },
        });
    }

    protected fechaHora(m: MensajeVoz): string {
        const f = new Date(m.created_at);
        return `${f.toLocaleDateString('es-CO')} ${f.toLocaleTimeString('es-CO', {
            hour: '2-digit',
            minute: '2-digit',
        })}`;
    }

    /** Solo se reproduce una dirección http o https (el backend entrega una URL firmada). */
    protected urlSegura(url: string): string | null {
        try {
            const u = new URL(url);
            return u.protocol === 'https:' || u.protocol === 'http:' ? u.toString() : null;
        } catch {
            return null;
        }
    }
}