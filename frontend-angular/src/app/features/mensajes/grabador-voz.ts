import { HttpErrorResponse } from '@angular/common/http';
import {
    ChangeDetectionStrategy,
    Component,
    DestroyRef,
    inject,
    input,
    signal,
} from '@angular/core';

import { Auth } from '../../core/auth';
import { MensajesVoz } from './mensajes-voz';

type Estado = 'inactivo' | 'grabando' | 'listo' | 'enviando' | 'enviado';

/** PROPUESTA: el contrato no fija una duración; 60 s mantienen el audio bajo el máximo de 5 MB. */
const SEGUNDOS_MAXIMOS = 60;
const BYTES_MAXIMOS = 5 * 1024 * 1024;

@Component({
    selector: 'app-grabador-voz',
    changeDetection: ChangeDetectionStrategy.OnPush,
    styleUrl: '../../shared/formulario.css',
    template: `
    <section class="voz" aria-labelledby="titulo-voz">
      <h2 id="titulo-voz">Mensaje de voz</h2>

      @if (!soportado) {
        <p>Tu navegador no puede grabar mensajes de voz.</p>
      } @else {
        @switch (estado()) {
          @case ('inactivo') {
            <button type="button" class="entrar secundario" (click)="grabar()">
              Grabar mensaje de voz
            </button>
          }
          @case ('grabando') {
            <p role="status">Grabando… (máximo {{ maximo }} segundos)</p>
            <button type="button" class="entrar" (click)="detener()">Detener grabación</button>
          }
          @case ('listo') {
            @if (vistaPrevia(); as url) {
              <audio controls [src]="url">Tu navegador no puede reproducir este audio.</audio>
            }
            <button type="button" class="entrar" (click)="enviar()">Enviar mensaje de voz</button>
            <button type="button" class="entrar secundario" (click)="descartar()">
              Grabar de nuevo
            </button>
          }
          @case ('enviando') {
            <p role="status">Enviando mensaje de voz…</p>
          }
          @case ('enviado') {
            <p class="confirmacion" role="status">Mensaje de voz enviado.</p>
            <button type="button" class="entrar secundario" (click)="descartar()">
              Grabar otro
            </button>
          }
        }
      }

      @if (error()) {
        <p class="error-general" role="alert">{{ error() }}</p>
      }
    </section>
  `,
})
export class GrabadorVoz {
    private readonly servicio = inject(MensajesVoz);
    private readonly auth = inject(Auth);

    readonly contactoId = input.required<string>();

    protected readonly maximo = SEGUNDOS_MAXIMOS;
    protected readonly soportado =
        typeof MediaRecorder !== 'undefined' && !!navigator.mediaDevices?.getUserMedia;

    protected readonly estado = signal<Estado>('inactivo');
    protected readonly error = signal('');
    protected readonly vistaPrevia = signal<string | null>(null);

    private grabadora: MediaRecorder | null = null;
    private flujo: MediaStream | null = null;
    private trozos: Blob[] = [];
    private audio: Blob | null = null;
    private temporizador: ReturnType<typeof setTimeout> | null = null;

    constructor() {
        inject(DestroyRef).onDestroy(() => {
            this.liberar();
            this.quitarVistaPrevia();
        });
    }

    protected async grabar(): Promise<void> {
        this.error.set('');
        try {
            this.flujo = await navigator.mediaDevices.getUserMedia({ audio: true });
        } catch {
            this.error.set('No pudimos usar el micrófono. Revisa el permiso del navegador.');
            return;
        }

        const tipo = ['audio/webm;codecs=opus', 'audio/webm'].find((t) =>
            MediaRecorder.isTypeSupported(t),
        );
        if (!tipo) {
            this.liberar();
            this.error.set('Tu navegador no puede grabar en el formato que acepta la aplicación.');
            return;
        }

        this.trozos = [];
        const grabadora = new MediaRecorder(this.flujo, { mimeType: tipo });
        grabadora.ondataavailable = (evento) => {
            if (evento.data.size > 0) {
                this.trozos.push(evento.data);
            }
        };
        grabadora.onstop = () => {
            this.liberar();
            const audio = new Blob(this.trozos, { type: 'audio/webm' });
            if (audio.size > BYTES_MAXIMOS) {
                this.error.set('El audio es demasiado largo. Graba uno más corto.');
                this.estado.set('inactivo');
                return;
            }
            this.audio = audio;
            this.quitarVistaPrevia();
            this.vistaPrevia.set(URL.createObjectURL(audio));
            this.estado.set('listo');
        };

        this.grabadora = grabadora;
        grabadora.start();
        this.estado.set('grabando');
        this.temporizador = setTimeout(() => this.detener(), SEGUNDOS_MAXIMOS * 1000);
    }

    protected detener(): void {
        if (this.grabadora && this.grabadora.state !== 'inactive') {
            this.grabadora.stop();
        }
    }

    protected descartar(): void {
        this.audio = null;
        this.quitarVistaPrevia();
        this.error.set('');
        this.estado.set('inactivo');
    }

    protected enviar(): void {
        if (!this.audio) {
            return;
        }
        this.estado.set('enviando');
        this.error.set('');
        this.servicio.enviar(this.contactoId(), this.audio).subscribe({
            next: () => {
                this.audio = null;
                this.quitarVistaPrevia();
                this.estado.set('enviado');
            },
            error: (error: HttpErrorResponse) => {
                if (error.status === 401) {
                    this.auth.cerrarSesion();
                    return;
                }
                this.estado.set('listo');
                this.error.set(
                    error.status === 403
                        ? 'No tienes permiso para enviar mensajes a este contacto.'
                        : error.status === 422
                            ? 'El audio no es válido o es demasiado grande. Graba otro.'
                            : 'No pudimos enviar el mensaje de voz. Inténtalo de nuevo.',
                );
            },
        });
    }

    private liberar(): void {
        if (this.temporizador) {
            clearTimeout(this.temporizador);
            this.temporizador = null;
        }
        this.flujo?.getTracks().forEach((pista) => pista.stop());
        this.flujo = null;
    }

    private quitarVistaPrevia(): void {
        const url = this.vistaPrevia();
        if (url) {
            URL.revokeObjectURL(url);
        }
        this.vistaPrevia.set(null);
    }
}