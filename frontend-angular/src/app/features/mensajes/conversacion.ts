import { HttpErrorResponse } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { FormControl, ReactiveFormsModule, Validators } from '@angular/forms';
import { ActivatedRoute, RouterLink } from '@angular/router';

import { Auth } from '../../core/auth';
import { Mensaje, Mensajes, horaMensaje } from './mensajes';

type Estado = 'cargando' | 'error' | 'listo';

@Component({
    selector: 'app-conversacion',
    imports: [ReactiveFormsModule, RouterLink],
    changeDetection: ChangeDetectionStrategy.OnPush,
    styleUrls: ['../../shared/formulario.css'],
    styles: [
        `
      .burbujas {
        list-style: none;
        padding: 0;
        margin: 0 0 1rem;
        display: flex;
        flex-direction: column;
        gap: 0.75rem;
      }
      .burbuja {
        max-width: 85%;
        padding: 0.75rem 1rem;
        border-radius: 1rem;
        background: #eef1f6;
        color: #1f2937;
        align-self: flex-start;
        overflow-wrap: anywhere;
      }
      .burbuja.mia {
        align-self: flex-end;
        background: var(--color-primary, #0d4abd);
        color: #ffffff;
      }
      .burbuja p {
        margin: 0;
      }
      .burbuja .hora {
        margin-top: 0.25rem;
        font-size: 0.875rem;
      }
      .escribir {
        display: flex;
        flex-direction: column;
        gap: 0.5rem;
        margin-block-end: 1rem;
      }
      .escribir label {
        font-weight: 600;
      }
      .escribir input {
        min-height: var(--touch-target, 3.5rem);
        padding: 0 1rem;
        font: inherit;
        border: 2px solid #5f6368;
        border-radius: 0.75rem;
      }
    `,
    ],
    template: `
    <main class="pagina">
      <h1>Conversación</h1>

      @switch (estado()) {
        @case ('cargando') {
          <p role="status">Cargando mensajes…</p>
        }
        @case ('error') {
          <p class="error-general" role="alert">{{ mensajeError() }}</p>
          <button type="button" class="entrar secundario" (click)="cargar()">
            Intentar de nuevo
          </button>
        }
        @case ('listo') {
          @if (lista().length === 0) {
            <p>Todavía no hay mensajes. ¡Escribe el primero!</p>
          } @else {
            <ul class="burbujas" aria-live="polite">
              @for (m of lista(); track m.id) {
                <li class="burbuja" [class.mia]="m.from_me">
                  <p>
                    <span class="sr-only">{{ m.from_me ? 'Tú: ' : 'Contacto: ' }}</span>
                    {{ m.text }}
                  </p>
                  <p class="hora">{{ hora(m) }}</p>
                </li>
              }
            </ul>
          }

          <button type="button" class="entrar secundario" (click)="cargar()">Actualizar</button>

          <form class="escribir" (ngSubmit)="enviar()" novalidate>
            <label for="texto">Escribe tu mensaje</label>
            <input id="texto" type="text" [formControl]="texto" autocomplete="off" />
            @if (errorEnvio()) {
              <p class="error-general" role="alert">{{ errorEnvio() }}</p>
            }
            <button type="submit" class="entrar" [disabled]="enviando()">
              {{ enviando() ? 'Enviando…' : 'Enviar' }}
            </button>
          </form>
        }
      }

      <a class="enlace" routerLink="/mensajes">Volver</a>
    </main>
  `,
})
export class Conversacion {
    private readonly ruta = inject(ActivatedRoute);
    private readonly servicio = inject(Mensajes);
    private readonly auth = inject(Auth);

    private readonly contactoId = this.ruta.snapshot.paramMap.get('contactoId') ?? '';

    protected readonly estado = signal<Estado>('cargando');
    protected readonly lista = signal<Mensaje[]>([]);
    protected readonly mensajeError = signal('');
    protected readonly enviando = signal(false);
    protected readonly errorEnvio = signal('');

    protected readonly texto = new FormControl('', {
        nonNullable: true,
        validators: [Validators.required, Validators.pattern(/\S/)],
    });

    constructor() {
        this.cargar();
    }

    protected cargar(): void {
        this.estado.set('cargando');
        this.servicio.conversacion(this.contactoId).subscribe({
            next: (mensajes) => {
                this.lista.set(
                    [...mensajes].sort(
                        (a, b) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime(),
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
                        ? 'No tienes permiso para ver esta conversación.'
                        : 'No pudimos cargar los mensajes. Revisa tu conexión e inténtalo de nuevo.',
                );
                this.estado.set('error');
            },
        });
    }

    protected hora(m: Mensaje): string {
        return horaMensaje(m.created_at);
    }

    protected enviar(): void {
        if (this.texto.invalid) {
            this.errorEnvio.set('Escribe un mensaje antes de enviar.');
            return;
        }
        const contenido = this.texto.value.trim();
        this.enviando.set(true);
        this.errorEnvio.set('');
        this.servicio.enviar(this.contactoId, contenido).subscribe({
            next: (enviado) => {
                this.lista.update((actual) => [
                    ...actual,
                    { id: enviado.id, from_me: true, text: contenido, created_at: enviado.created_at },
                ]);
                this.texto.reset('');
                this.enviando.set(false);
            },
            error: (error: HttpErrorResponse) => {
                this.enviando.set(false);
                if (error.status === 401) {
                    this.auth.cerrarSesion();
                    return;
                }
                this.errorEnvio.set(
                    error.status === 403
                        ? 'No tienes permiso para enviar mensajes a este contacto.'
                        : 'No pudimos enviar el mensaje. Inténtalo de nuevo.',
                );
            },
        });
    }
}
