import { HttpErrorResponse } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { Router, RouterLink } from '@angular/router';

import { Auth } from '../../core/auth';
import { correoDuplicado } from '../../core/errores';

type Campo = 'first_name' | 'last_name' | 'email' | 'password' | 'repetir';

@Component({
  selector: 'app-registro',
  imports: [ReactiveFormsModule, RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  styleUrl: '../../shared/formulario.css',
  template: `
    <main class="pagina">
      <h1>Crear cuenta de familiar</h1>
      <p>Con esta cuenta podrás registrar y acompañar a tu adulto mayor.</p>

      <form [formGroup]="formulario" (ngSubmit)="crear()" novalidate>
        <p class="nota"><span class="obligatorio" aria-hidden="true">*</span> Campo obligatorio</p>
        <div class="campo">
          <label for="nombre">Nombre <span class="obligatorio" aria-hidden="true">*</span></label>
          <input
            id="nombre" aria-required="true"
            type="text"
            formControlName="first_name"
            autocomplete="given-name"
            [attr.aria-invalid]="mostrarError('first_name') ? 'true' : null"
            [attr.aria-describedby]="mostrarError('first_name') ? 'error-nombre' : null"
          />
          @if (mostrarError('first_name')) {
            <p id="error-nombre" class="error">Escribe tu nombre.</p>
          }
        </div>

        <div class="campo">
          <label for="apellido">Apellido <span class="obligatorio" aria-hidden="true">*</span></label>
          <input
            id="apellido" aria-required="true"
            type="text"
            formControlName="last_name"
            autocomplete="family-name"
            [attr.aria-invalid]="mostrarError('last_name') ? 'true' : null"
            [attr.aria-describedby]="mostrarError('last_name') ? 'error-apellido' : null"
          />
          @if (mostrarError('last_name')) {
            <p id="error-apellido" class="error">Escribe tu apellido.</p>
          }
        </div>

        <div class="campo">
          <label for="correo">Correo electrónico <span class="obligatorio" aria-hidden="true">*</span></label>
          <input
            id="correo" aria-required="true"
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
          <label for="clave">Contraseña <span class="obligatorio" aria-hidden="true">*</span></label>
          <input
            id="clave" aria-required="true"
            type="password"
            formControlName="password"
            autocomplete="new-password"
            [attr.aria-invalid]="mostrarError('password') ? 'true' : null"
            [attr.aria-describedby]="mostrarError('password') ? 'error-clave' : null"
          />
          @if (mostrarError('password')) {
            <p id="error-clave" class="error">La contraseña debe tener al menos 6 caracteres.</p>
          }
        </div>

        <div class="campo">
          <label for="repetir">Repite la contraseña <span class="obligatorio" aria-hidden="true">*</span></label>
          <input
            id="repetir" aria-required="true"
            type="password"
            formControlName="repetir"
            autocomplete="new-password"
            [attr.aria-invalid]="mostrarErrorRepetir() ? 'true' : null"
            [attr.aria-describedby]="mostrarErrorRepetir() ? 'error-repetir' : null"
          />
          @if (mostrarErrorRepetir()) {
            <p id="error-repetir" class="error">Las dos contraseñas deben ser iguales.</p>
          }
        </div>

        @if (mensajeError()) {
          <p class="error-general" role="alert">{{ mensajeError() }}</p>
        }

        <button type="submit" class="entrar" [disabled]="cargando()">
          {{ cargando() ? 'Creando cuenta…' : 'Crear cuenta' }}
        </button>
      </form>

      <a class="enlace" routerLink="/login">Ya tengo cuenta</a>
    </main>
  `,
})
export class Registro {
  private readonly auth = inject(Auth);
  private readonly router = inject(Router);

  protected readonly formulario = new FormGroup({
    first_name: new FormControl('', { nonNullable: true, validators: [Validators.required] }),
    last_name: new FormControl('', { nonNullable: true, validators: [Validators.required] }),
    email: new FormControl('', {
      nonNullable: true,
      validators: [Validators.required, Validators.minLength(3), Validators.email],
    }),
    password: new FormControl('', {
      nonNullable: true,
      validators: [Validators.required, Validators.minLength(6)],
    }),
    repetir: new FormControl('', { nonNullable: true, validators: [Validators.required] }),
  });

  protected readonly cargando = signal(false);
  protected readonly mensajeError = signal('');

  protected mostrarError(campo: Campo): boolean {
    const control = this.formulario.controls[campo];
    return control.invalid && control.touched;
  }

  protected mostrarErrorRepetir(): boolean {
    const control = this.formulario.controls.repetir;
    return control.touched && (control.invalid || !this.coinciden());
  }

  protected crear(): void {
    this.formulario.markAllAsTouched();
    if (this.formulario.invalid || !this.coinciden() || this.cargando()) {
      return;
    }

    this.cargando.set(true);
    this.mensajeError.set('');
    const { first_name, last_name, email, password } = this.formulario.getRawValue();

    const correo = email.trim();
    this.auth
      .registrarSinSesion({
        first_name: first_name.trim(),
        last_name: last_name.trim(),
        email: correo,
        password,
      })
      .subscribe({
        next: () => {
          // No se entra todavía: el backend exige verificar el correo antes de iniciar sesión.
          this.cargando.set(false);
          this.auth.avisarEnInicio(
            `Te enviamos un correo de verificación a ${correo}. Ábrelo y luego inicia sesión.`,
          );
          this.router.navigateByUrl('/login');
        },
        error: (error: HttpErrorResponse) => {
          this.cargando.set(false);
          this.mensajeError.set(this.traducirError(error));
        },
      });
  }

  private coinciden(): boolean {
    const { password, repetir } = this.formulario.getRawValue();
    return password === repetir;
  }

  private traducirError(error: HttpErrorResponse): string {
    if (correoDuplicado(error)) {
      return 'Ya existe una cuenta con ese correo. Prueba iniciar sesión.';
    }
    switch (error.status) {
      case 422:
        return 'Revisa los datos: alguno no es válido.';
      case 429:
        return 'Hiciste muchos intentos. Espera unos minutos e inténtalo otra vez.';
      case 503:
        return 'El servicio no está disponible ahora. Inténtalo más tarde.';
      default:
        return 'No pudimos conectar con el servidor. Revisa tu conexión e inténtalo de nuevo.';
    }
  }
}