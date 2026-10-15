import { HttpErrorResponse } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { Router } from '@angular/router';

import { Auth } from '../../core/auth';

type CampoLogin = 'email' | 'password';

@Component({
    selector: 'app-login',
    imports: [ReactiveFormsModule],
    changeDetection: ChangeDetectionStrategy.OnPush,
    template: `
    <header class="encabezado">
      <p class="marca">Connects Senior</p>
    </header>

    <main class="contenido">
      <h1>Iniciar sesión</h1>
      <p>Escribe tu correo y tu contraseña para entrar.</p>

      <form [formGroup]="formulario" (ngSubmit)="entrar()" novalidate>
        <div class="campo">
          <label for="correo">Correo electrónico</label>
          <input
            id="correo"
            type="email"
            formControlName="email"
            autocomplete="email"
            inputmode="email"
            [attr.aria-invalid]="mostrarError('email') ? 'true' : null"
            [attr.aria-describedby]="mostrarError('email') ? 'error-correo' : null"
          />
          @if (mostrarError('email')) {
            <p id="error-correo" class="error">
              Escribe un correo válido, por ejemplo nombre&#64;correo.com
            </p>
          }
        </div>

        <div class="campo">
          <label for="clave">Contraseña</label>
          <div class="con-boton">
            <input
              id="clave"
              [type]="verContrasena() ? 'text' : 'password'"
              formControlName="password"
              autocomplete="current-password"
              [attr.aria-invalid]="mostrarError('password') ? 'true' : null"
              [attr.aria-describedby]="mostrarError('password') ? 'error-clave' : null"
            />
            <button
              type="button"
              class="ver"
              (click)="verContrasena.set(!verContrasena())"
              [attr.aria-label]="verContrasena() ? 'Ocultar contraseña' : 'Mostrar contraseña'"
            >
              {{ verContrasena() ? 'Ocultar' : 'Mostrar' }}
            </button>
          </div>
          @if (mostrarError('password')) {
            <p id="error-clave" class="error">La contraseña debe tener al menos 6 caracteres.</p>
          }
        </div>

        @if (mensajeError()) {
          <p class="error-general" role="alert">{{ mensajeError() }}</p>
        }

        <button type="submit" class="entrar" [disabled]="cargando()">
          {{ cargando() ? 'Entrando…' : 'Entrar' }}
        </button>
      </form>
    </main>
  `,
    styles: `
    .encabezado {
      padding: var(--space-3);
      background: var(--color-primary);
      color: var(--color-primary-contrast);
      border-radius: 0 0 var(--radius) var(--radius);
    }
    .marca {
      max-width: 28rem;
      margin: 0 auto;
      font-size: 1.25rem;
      font-weight: 700;
    }
    .contenido {
      max-width: 28rem;
      margin: 0 auto;
      padding: var(--space-3);
    }
    .campo {
      margin-bottom: var(--space-3);
    }
    label {
      display: block;
      margin-bottom: var(--space-1);
      font-weight: 700;
      color: var(--color-heading);
    }
    input {
      width: 100%;
      min-height: var(--touch-target);
      padding: 0 var(--space-2);
      border: 2px solid #5f6f8f;
      border-radius: var(--radius);
      background: var(--color-surface);
      color: var(--color-text);
      font: inherit;
    }
    input[aria-invalid='true'] {
      border-color: #b3141b;
    }
    .con-boton {
      display: flex;
      gap: var(--space-1);
    }
    .ver {
      flex: none;
      min-height: var(--touch-target);
      padding: 0 var(--space-2);
      border: 2px solid var(--color-primary);
      border-radius: var(--radius);
      background: var(--color-surface);
      color: var(--color-primary);
      font: inherit;
      font-weight: 700;
      cursor: pointer;
    }
    .error {
      margin: var(--space-1) 0 0;
      color: #b3141b;
      font-weight: 700;
    }
    .error-general {
      padding: var(--space-2);
      border: 2px solid #b3141b;
      border-radius: var(--radius);
      background: #fff0f0;
      color: #7a0c11;
      font-weight: 700;
    }
    .entrar {
      width: 100%;
      min-height: var(--touch-target);
      border: 0;
      border-radius: var(--radius);
      background: var(--color-primary);
      color: var(--color-primary-contrast);
      font: inherit;
      font-weight: 700;
      cursor: pointer;
    }
    .entrar:disabled {
      opacity: 0.6;
      cursor: wait;
    }
  `,
})
export class Login {
    private readonly auth = inject(Auth);
    private readonly router = inject(Router);

    protected readonly formulario = new FormGroup({
        email: new FormControl('', {
            nonNullable: true,
            validators: [Validators.required, Validators.minLength(3), Validators.email],
        }),
        password: new FormControl('', {
            nonNullable: true,
            validators: [Validators.required, Validators.minLength(6)],
        }),
    });

    protected readonly cargando = signal(false);
    protected readonly verContrasena = signal(false);
    protected readonly mensajeError = signal('');

    protected mostrarError(campo: CampoLogin): boolean {
        const control = this.formulario.controls[campo];
        return control.invalid && control.touched;
    }

    protected entrar(): void {
        this.formulario.markAllAsTouched();
        if (this.formulario.invalid || this.cargando()) {
            return;
        }

        this.cargando.set(true);
        this.mensajeError.set('');
        const { email, password } = this.formulario.getRawValue();

        this.auth.login(email.trim(), password).subscribe({
            next: () => {
                this.cargando.set(false);
                this.router.navigateByUrl('/inicio');
            },
            error: (error: HttpErrorResponse) => {
                this.cargando.set(false);
                this.mensajeError.set(this.traducirError(error.status));
            },
        });
    }

    private traducirError(estado: number): string {
        switch (estado) {
            case 401:
                return 'El correo o la contraseña no son correctos. Revísalos e inténtalo de nuevo.';
            case 403:
                return 'Tu cuenta está deshabilitada. Habla con tu familiar responsable.';
            case 429:
                return 'Hiciste muchos intentos. Espera unos minutos e inténtalo otra vez.';
            case 503:
                return 'El servicio de acceso no está disponible ahora. Inténtalo más tarde.';
            default:
                return 'No pudimos conectar con el servidor. Revisa tu conexión e inténtalo de nuevo.';
        }
    }
}