import { HttpErrorResponse } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';

import { Auth } from '../../core/auth';
import { Contactos, DatosContacto } from './contactos';

type Campo = 'first_name' | 'last_name' | 'relationship' | 'email';

@Component({
    selector: 'app-agregar-contacto',
    imports: [ReactiveFormsModule, RouterLink],
    changeDetection: ChangeDetectionStrategy.OnPush,
    styleUrl: '../../shared/formulario.css',
    template: `
    <main class="pagina">
      <h1>{{ editando ? 'Editar contacto' : 'Agregar contacto' }}</h1>

      @if (cargandoDatos()) {
        <p role="status">Cargando…</p>
      } @else if (errorCarga()) {
        <p class="error-general" role="alert">{{ errorCarga() }}</p>
      } @else {
        <form [formGroup]="formulario" (ngSubmit)="guardar()" novalidate>
          <div class="campo">
            <label for="nombre">Nombre</label>
            <input
              id="nombre"
              type="text"
              formControlName="first_name"
              autocomplete="off"
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
              autocomplete="off"
              [attr.aria-invalid]="mostrarError('last_name') ? 'true' : null"
              [attr.aria-describedby]="mostrarError('last_name') ? 'error-apellido' : null"
            />
            @if (mostrarError('last_name')) {
              <p id="error-apellido" class="error">Escribe el apellido.</p>
            }
          </div>

          <div class="campo">
            <label for="relacion">Relación (por ejemplo: Hija, Hijo, Cuidadora)</label>
            <input
              id="relacion"
              type="text"
              formControlName="relationship"
              autocomplete="off"
              [attr.aria-invalid]="mostrarError('relationship') ? 'true' : null"
              [attr.aria-describedby]="mostrarError('relationship') ? 'error-relacion' : null"
            />
            @if (mostrarError('relationship')) {
              <p id="error-relacion" class="error">Escribe la relación con el adulto mayor.</p>
            }
          </div>

          <div class="campo">
            <label for="telefono">Teléfono (opcional)</label>
            <input
              id="telefono"
              type="tel"
              formControlName="phone"
              inputmode="tel"
              autocomplete="off"
            />
          </div>

          <div class="campo">
            <label for="correo">Correo electrónico (opcional)</label>
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

          <fieldset class="permisos">
            <legend>Permisos del contacto</legend>
            <label class="permiso">
              <input type="checkbox" formControlName="calls" />
              Llamadas
            </label>
            <label class="permiso">
              <input type="checkbox" formControlName="video_calls" />
              Videollamadas
            </label>
            <label class="permiso">
              <input type="checkbox" formControlName="messages" />
              Mensajes
            </label>
            <label class="permiso">
              <input type="checkbox" formControlName="help_alerts" />
              Recibir alertas de ayuda
            </label>
            <p class="nota">Los contactos con alertas de ayuda son los contactos de confianza.</p>
          </fieldset>

          @if (mensajeError()) {
            <p class="error-general" role="alert">{{ mensajeError() }}</p>
          }

          <button type="submit" class="entrar" [disabled]="cargando()">
            {{ cargando() ? 'Guardando…' : 'Guardar contacto' }}
          </button>
        </form>
      }

      <a class="enlace" [routerLink]="rutaLista">Cancelar</a>
    </main>
  `,
})
export class AgregarContacto {
    private readonly ruta = inject(ActivatedRoute);
    private readonly router = inject(Router);
    private readonly contactos = inject(Contactos);
    private readonly auth = inject(Auth);

    protected readonly adultoId = this.ruta.snapshot.paramMap.get('adultoId') ?? '';
    private readonly contactoId = this.ruta.snapshot.paramMap.get('id');
    protected readonly editando = this.contactoId !== null;
    protected readonly rutaLista = ['/adultos', this.adultoId, 'contactos'];

    protected readonly formulario = new FormGroup({
        first_name: new FormControl('', { nonNullable: true, validators: [Validators.required] }),
        last_name: new FormControl('', { nonNullable: true, validators: [Validators.required] }),
        relationship: new FormControl('', { nonNullable: true, validators: [Validators.required] }),
        phone: new FormControl('', { nonNullable: true }),
        email: new FormControl('', { nonNullable: true, validators: [Validators.email] }),
        calls: new FormControl(true, { nonNullable: true }),
        video_calls: new FormControl(true, { nonNullable: true }),
        messages: new FormControl(true, { nonNullable: true }),
        help_alerts: new FormControl(true, { nonNullable: true }),
    });

    protected readonly cargandoDatos = signal(this.editando);
    protected readonly errorCarga = signal('');
    protected readonly cargando = signal(false);
    protected readonly mensajeError = signal('');

    constructor() {
        if (this.editando) {
            this.cargarContacto();
        }
    }

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
        const v = this.formulario.getRawValue();

        const datos: DatosContacto = {
            first_name: v.first_name.trim(),
            last_name: v.last_name.trim(),
            relationship: v.relationship.trim(),
            phone: v.phone.trim() || null,
            email: v.email.trim() || null,
            permissions: {
                calls: v.calls,
                video_calls: v.video_calls,
                messages: v.messages,
                help_alerts: v.help_alerts,
            },
        };

        const peticion = this.contactoId
            ? this.contactos.actualizar(this.contactoId, datos)
            : this.contactos.crear({ ...datos, adult_id: this.adultoId });

        peticion.subscribe({
            next: () => {
                this.cargando.set(false);
                this.router.navigate(this.rutaLista);
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

    private cargarContacto(): void {
        this.contactos.listar(this.adultoId).subscribe({
            next: (lista) => {
                const contacto = lista.find((c) => c.id === this.contactoId);
                if (!contacto) {
                    this.errorCarga.set('No encontramos este contacto.');
                    this.cargandoDatos.set(false);
                    return;
                }
                this.formulario.patchValue({
                    first_name: contacto.first_name,
                    last_name: contacto.last_name,
                    relationship: contacto.relationship,
                    phone: contacto.phone ?? '',
                    email: contacto.email ?? '',
                    calls: contacto.permissions.calls,
                    video_calls: contacto.permissions.video_calls,
                    messages: contacto.permissions.messages,
                    help_alerts: contacto.permissions.help_alerts,
                });
                this.cargandoDatos.set(false);
            },
            error: (error: HttpErrorResponse) => {
                if (error.status === 401) {
                    this.auth.cerrarSesion();
                    return;
                }
                this.errorCarga.set('No pudimos cargar el contacto. Revisa tu conexión e inténtalo de nuevo.');
                this.cargandoDatos.set(false);
            },
        });
    }

    private traducirError(estado: number): string {
        switch (estado) {
            case 403:
                return 'No tienes permiso para gestionar los contactos de este adulto mayor.';
            case 404:
                return 'No encontramos este contacto.';
            case 422:
                return 'Revisa los datos: alguno no es válido.';
            case 503:
                return 'El servicio no está disponible ahora. Inténtalo más tarde.';
            default:
                return 'No pudimos guardar el contacto. Revisa tu conexión e inténtalo de nuevo.';
        }
    }
}