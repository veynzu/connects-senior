import { HttpErrorResponse } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { Router, RouterLink } from '@angular/router';

import { Auth } from '../../core/auth';
import { Adultos, NuevoAdulto } from './adultos';

type Campo = 'first_name' | 'last_name' | 'email' | 'password';

@Component({
    selector: 'app-registrar-adulto',
    imports: [ReactiveFormsModule, RouterLink],
    changeDetection: ChangeDetectionStrategy.OnPush,
    styleUrl: '../../shared/formulario.css',
    template: `
    <main class="pagina">
      <h1>Registrar adulto mayor</h1>

      <form [formGroup]="formulario" (ngSubmit)="guardar()" novalidate>
        <div class="campo">
          <label for="nombre">Nombre</label>
          <input
            id="nombre"
            type="text"
            formControlName="first_name"
            [attr.aria-invalid]="mostrarError('first_name') ? 'true' : null"
            [attr.aria-describedby]="mostrarError('first_name') ? 'error-nombre' : null"
          />
          @if (mostrarError('first_name')) {
            <p id="error-nombre" class="error">Escribe el nombre.</p>
          }
        </div>

        <div class="campo">
          <label for="apellido">Apellido</label>
          <input
            id="apellido"
            type="text"
            formControlName="last_name"
            [attr.aria-invalid]="mostrarError('last_name') ? 'true' : null"
            [attr.aria-describedby]="mostrarError('last_name') ? 'error-apellido' : null"
          />
          @if (mostrarError('last_name')) {
            <p id="error-apellido" class="error">Escribe el apellido.</p>
          }
        </div>

        <div class="campo">
          <label for="correo">Correo electrónico</label>
          <input
            id="correo"
            type="email"
            formControlName="email"
            inputmode="email"
            autocomplete="off"
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
          <label for="telefono">Teléfono (opcional)</label>
          <input id="telefono" type="tel" formControlName="phone" inputmode="tel" autocomplete="off" />
        </div>

        <div class="campo">
          <label for="nacimiento">Fecha de nacimiento (opcional)</label>
          <input id="nacimiento" type="date" formControlName="birth_date" />
        </div>

        <div class="campo">
          <label for="direccion">Dirección registrada (opcional)</label>
          <input id="direccion" type="text" formControlName="address" autocomplete="off" />
        </div>

        <div class="campo">
          <label for="clave">Contraseña para que entre a su cuenta</label>
          <input
            id="clave"
            type="password"
            formControlName="password"
            autocomplete="new-password"
            [attr.aria-invalid]="mostrarError('password') ? 'true' : null"
            [attr.aria-describedby]="mostrarError('password') ? 'error-clave' : 'nota-clave'"
          />
          <p id="nota-clave" class="nota">
            Con su correo y esta contraseña, el adulto mayor iniciará sesión en su dispositivo.
          </p>
          @if (mostrarError('password')) {
            <p id="error-clave" class="error">La contraseña debe tener al menos 6 caracteres.</p>
          }
        </div>

        @if (mensajeError()) {
          <p class="error-general" role="alert">{{ mensajeError() }}</p>
        }

        <button type="submit" class="entrar" [disabled]="cargando()">
          {{ cargando() ? 'Guardando…' : 'Guardar y continuar' }}
        </button>
      </form>

      <a class="enlace" routerLink="/familiar">Cancelar</a>
    </main>
  `,
})
export class RegistrarAdulto {
    private readonly adultos = inject(Adultos);
    private readonly auth = inject(Auth);
    private readonly router = inject(Router);

    protected readonly formulario = new FormGroup({
        first_name: new FormControl('', { nonNullable: true, validators: [Validators.required] }),
        last_name: new FormControl('', { nonNullable: true, validators: [Validators.required] }),
        email: new FormControl('', {
            nonNullable: true,
            validators: [Validators.required, Validators.minLength(3), Validators.email],
        }),
        phone: new FormControl('', { nonNullable: true }),
        birth_date: new FormControl('', { nonNullable: true }),
        address: new FormControl('', { nonNullable: true }),
        password: new FormControl('', {
            nonNullable: true,
            validators: [Validators.required, Validators.minLength(6)],
        }),
    });

    protected readonly cargando = signal(false);
    protected readonly mensajeError = signal('');

    protected mostrarError(campo: Campo): boolean {
        const control = this.formulario.controls[campo];
        return control.invalid && control.touched;
    }

    protected guardar(): void {
        this.formulario.markAllAsTouched();
        if (this.formulario.invalid || this.cargando()) {
            return;
        }

        this.cargando.set(true);
        this.mensajeError.set('');
        const valores = this.formulario.getRawValue();

        const datos: NuevoAdulto = {
            first_name: valores.first_name.trim(),
            last_name: valores.last_name.trim(),
            email: valores.email.trim(),
            password: valores.password,
        };
        if (valores.phone.trim()) {
            datos.phone = valores.phone.trim();
        }
        if (valores.birth_date) {
            datos.birth_date = valores.birth_date;
        }
        if (valores.address.trim()) {
            datos.address = valores.address.trim();
        }

        this.adultos.crear(datos).subscribe({
            next: () => {
                this.cargando.set(false);
                this.router.navigateByUrl('/familiar');
            },
            error: (error: HttpErrorResponse) => {
                this.cargando.set(false);
                if (error.status === 401) {
                    this.auth.cerrarSesion();
                    return;
                }
                this.mensajeError.set(this.traducirError(error.status));
            },
        });
    }

    private traducirError(estado: number): string {
        switch (estado) {
            case 403:
                return 'Solo un familiar puede registrar adultos mayores.';
            case 409:
                return 'Ya existe una cuenta con ese correo.';
            case 422:
                return 'Revisa los datos: alguno no es válido.';
            case 503:
                return 'El servicio no está disponible ahora. Inténtalo más tarde.';
            default:
                return 'No pudimos guardar los datos. Revisa tu conexión e inténtalo de nuevo.';
        }
    }
}