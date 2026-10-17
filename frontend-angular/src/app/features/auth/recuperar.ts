import { HttpErrorResponse } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { FormControl, ReactiveFormsModule, Validators } from '@angular/forms';
import { RouterLink } from '@angular/router';

import { Auth } from '../../core/auth';

@Component({
    selector: 'app-recuperar',
    imports: [ReactiveFormsModule, RouterLink],
    changeDetection: ChangeDetectionStrategy.OnPush,
    styleUrl: '../../shared/formulario.css',
    template: `
    <main class="pagina">
      <h1>Recuperar contraseña</h1>
      <p>Escribe tu correo y te enviaremos un mensaje para crear una contraseña nueva.</p>

      @if (enviado()) {
        <p class="exito-general" role="status">
          Si el correo está registrado, te enviamos un mensaje. Revisa tu bandeja de entrada.
        </p>
        <a class="entrar" routerLink="/login">Volver a iniciar sesión</a>
      } @else {
        <form (ngSubmit)="enviar()" novalidate>
          <div class="campo">
            <label for="correo">Correo electrónico</label>
            <input
              id="correo"
              type="email"
              [formControl]="correo"
              autocomplete="email"
              inputmode="email"
              [attr.aria-invalid]="correo.invalid && correo.touched ? 'true' : null"
              [attr.aria-describedby]="correo.invalid && correo.touched ? 'error-correo' : null"
            />
            @if (correo.invalid && correo.touched) {
              <p id="error-correo" class="error">
                Escribe un correo válido, por ejemplo nombre&#64;correo.com
              </p>
            }
          </div>

          @if (mensajeError()) {
            <p class="error-general" role="alert">{{ mensajeError() }}</p>
          }

          <button type="submit" class="entrar" [disabled]="cargando()">
            {{ cargando() ? 'Enviando…' : 'Enviar mensaje' }}
          </button>
        </form>

        <a class="enlace" routerLink="/login">Volver a iniciar sesión</a>
      }
    </main>
  `,
})
export class Recuperar {
    private readonly auth = inject(Auth);

    protected readonly correo = new FormControl('', {
        nonNullable: true,
        validators: [Validators.required, Validators.minLength(3), Validators.email],
    });

    protected readonly cargando = signal(false);
    protected readonly enviado = signal(false);
    protected readonly mensajeError = signal('');

    protected enviar(): void {
        this.correo.markAsTouched();
        if (this.correo.invalid || this.cargando()) {
            return;
        }

        this.cargando.set(true);
        this.mensajeError.set('');

        this.auth.recuperar(this.correo.value.trim()).subscribe({
            next: () => {
                this.cargando.set(false);
                this.enviado.set(true);
            },
            error: (error: HttpErrorResponse) => {
                this.cargando.set(false);
                this.mensajeError.set(this.traducirError(error.status));
            },
        });
    }

    private traducirError(estado: number): string {
        switch (estado) {
            case 429:
                return 'Hiciste muchos intentos. Espera unos minutos e inténtalo otra vez.';
            case 503:
                return 'El servicio no está disponible ahora. Inténtalo más tarde.';
            default:
                return 'No pudimos conectar con el servidor. Revisa tu conexión e inténtalo de nuevo.';
        }
    }
}