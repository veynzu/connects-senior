import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { DomSanitizer, SafeResourceUrl } from '@angular/platform-browser';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { catchError, forkJoin, of } from 'rxjs';

import { API_BASE_URL } from '../../core/api-config';
import { Auth } from '../../core/auth';
import { Contacto, Contactos } from '../contactos/contactos';
import { LlamadaIniciada, Llamadas } from './llamadas';

type Estado = 'conectando' | 'activa' | 'error';

@Component({
  selector: 'app-llamada',
  imports: [RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <main class="pantalla">
      <h1>{{ soloAudio ? 'Llamada con' : 'Videollamada con' }} {{ nombre() }}</h1>

      @switch (estado()) {
        @case ('conectando') {
          <p role="status" class="mensaje">Conectando…</p>
          <button type="button" class="finalizar" (click)="colgar()">Colgar</button>
        }
        @case ('activa') {
          @if (sala(); as url) {
            <iframe
              class="sala"
              [src]="url"
              title="Sala de la llamada"
              allow="camera; microphone; autoplay; display-capture; fullscreen"
            ></iframe>
          }
          <p role="status" class="mensaje">Comunicación en curso</p>
          <button type="button" class="finalizar" (click)="colgar()">Colgar</button>
        }
        @case ('error') {
          <div class="error" role="alert">
            <p>{{ mensajeError() }}</p>
          </div>
          <button type="button" class="reintentar" (click)="iniciar()">Intentar de nuevo</button>
          @if (servicioNoDisponible()) {
            <a class="volver" [routerLink]="['/mensajes', contactoId]">Dejar un mensaje de voz</a>
          }
          <a class="volver" routerLink="/contactos">Volver a mis contactos</a>
        }
      }
    </main>
  `,
  styles: `
    :host {
      display: block;
      min-height: 100vh;
      background: #071a45;
      color: #fff;
    }
    .pantalla {
      max-width: 28rem;
      margin: 0 auto;
      padding: var(--space-3);
    }
    h1 {
      color: #fff;
    }
    .mensaje {
      text-align: center;
      font-weight: 700;
    }
    .sala {
      display: block;
      width: 100%;
      height: 60vh;
      min-height: 20rem;
      border: 0;
      border-radius: var(--radius);
      background: #000;
    }
    .finalizar,
    .reintentar,
    .volver {
      display: flex;
      align-items: center;
      justify-content: center;
      width: 100%;
      min-height: var(--touch-target);
      margin-top: var(--space-2);
      border: 2px solid transparent;
      border-radius: var(--radius);
      font: inherit;
      font-weight: 700;
      text-decoration: none;
      cursor: pointer;
    }
    .finalizar {
      background: var(--color-danger);
      color: #fff;
    }
    .reintentar {
      background: #fff;
      color: var(--color-primary);
    }
    .volver {
      border-color: #fff;
      background: transparent;
      color: #fff;
    }
    .error {
      padding: var(--space-2);
      border: 2px solid #ffb3b6;
      border-radius: var(--radius);
      background: #fff0f0;
      color: #7a0c11;
      font-weight: 700;
    }
  `,
})
export class Llamada {
  private readonly ruta = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly http = inject(HttpClient);
  private readonly llamadas = inject(Llamadas);
  private readonly contactos = inject(Contactos);
  private readonly auth = inject(Auth);
  private readonly sanitizer = inject(DomSanitizer);

  protected readonly contactoId = this.ruta.snapshot.paramMap.get('id') ?? '';
  protected readonly soloAudio = this.ruta.snapshot.queryParamMap.get('audio') === '1';

  protected readonly estado = signal<Estado>('conectando');
  protected readonly nombre = signal('');
  protected readonly sala = signal<SafeResourceUrl | null>(null);
  protected readonly mensajeError = signal('');
  protected readonly servicioNoDisponible = signal(false);

  private callId = '';
  /** Se marca si la persona cuelga mientras la llamada todavía se está creando. */
  private colgado = false;

  constructor() {
    this.iniciar();
  }

  protected iniciar(): void {
    this.estado.set('conectando');
    this.servicioNoDisponible.set(false);
    this.colgado = false;

    forkJoin({
      llamada: this.llamadas.iniciar(this.contactoId, this.soloAudio),
      lista: this.contactos.listar().pipe(catchError(() => of([] as Contacto[]))),
    }).subscribe({
      next: ({ llamada, lista }) => {
        if (this.colgado) {
          /* Colgó antes de que la llamada existiera: se cierra apenas se crea. */
          this.terminar(llamada.call_id, 'cancelada');
          return;
        }
        const direccion = this.direccionSegura(llamada);
        if (!direccion) {
          this.fallar(0);
          return;
        }
        const contacto = lista.find((c) => c.id === this.contactoId);
        if (contacto) {
          this.nombre.set(`${contacto.first_name} ${contacto.last_name}`);
        }
        this.callId = llamada.call_id;
        this.sala.set(this.sanitizer.bypassSecurityTrustResourceUrl(direccion));
        this.estado.set('activa');
      },
      error: (error: HttpErrorResponse) => {
        if (!this.colgado) {
          this.fallar(error.status);
        }
      },
    });
  }

  /**
   * El botón Colgar está disponible desde que empieza la pantalla (por si la llamada fue un error).
   * PROPUESTA: antes de que cargue la sala se envía "cancelada"; con la sala abierta, "completada".
   * El contrato aún no define cómo saber si el contacto llegó a unirse.
   */
  protected colgar(): void {
    const antesDeLaSala = this.estado() === 'conectando';
    this.colgado = true;
    if (this.callId) {
      this.terminar(this.callId, antesDeLaSala ? 'cancelada' : 'completada');
    }
    this.router.navigateByUrl('/contactos');
  }

  /** L2: PATCH /api/calls/{call_id} con { status }. Si falla, la persona igual sale de la llamada. */
  private terminar(callId: string, status: 'completada' | 'cancelada'): void {
    this.http
      .patch(`${API_BASE_URL}/api/calls/${callId}`, { status })
      .subscribe({ error: () => undefined });
  }

  /** Solo se acepta una sala con https; el token se agrega como parámetro "t". */
  private direccionSegura(llamada: LlamadaIniciada): string | null {
    try {
      const url = new URL(llamada.room_url);
      if (url.protocol !== 'https:') {
        return null;
      }
      if (llamada.token) {
        url.searchParams.set('t', llamada.token);
      }
      return url.toString();
    } catch {
      return null;
    }
  }

  private fallar(estado: number): void {
    if (estado === 401) {
      this.auth.cerrarSesion();
      return;
    }
    this.servicioNoDisponible.set(estado === 503);
    this.mensajeError.set(this.traducirError(estado));
    this.estado.set('error');
  }

  private traducirError(estado: number): string {
    switch (estado) {
      case 403:
        return 'No tienes permiso para llamar a este contacto.';
      case 404:
        return 'No encontramos a este contacto.';
      case 503:
        return 'El servicio de llamadas no está disponible ahora. Inténtalo más tarde o deja un mensaje de voz.';
      default:
        return 'No pudimos iniciar la llamada. Revisa tu conexión e inténtalo de nuevo.';
    }
  }
}