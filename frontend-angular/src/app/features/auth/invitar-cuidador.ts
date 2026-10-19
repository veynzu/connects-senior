import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, Injectable, inject, signal } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { Observable } from 'rxjs';

import { API_BASE_URL } from '../../core/api-config';
import { Auth } from '../../core/auth';

/*
 * Contrato v1.0, A9: POST /api/auth/adults/{adult_id}/caregivers.
 * Este endpoint todavía NO existe en el backend.
 */
export interface PermisosCuidador {
    manage_contacts: boolean;
    manage_reminders: boolean;
    receive_alerts: boolean;
}

export interface NuevoCuidador {
    first_name: string;
    last_name: string;
    email: string;
    permissions: PermisosCuidador;
}

export interface Cuidador extends NuevoCuidador {
    id: string;
}

@Injectable({ providedIn: 'root' })
export class Cuidadores {
    private readonly http = inject(HttpClient);

    invitar(adultId: string, datos: NuevoCuidador): Observable<Cuidador> {
        return this.http.post<Cuidador>(
            `${API_BASE_URL}/api/auth/adults/${adultId}/caregivers`,
            datos,
        );
    }
}

@Component({
    selector: 'app-invitar-cuidador',
    imports: [ReactiveFormsModule, RouterLink],
    changeDetection: ChangeDetectionStrategy.OnPush,
    styleUrls: ['../../shared/formulario.css'],
    styles: [
        `
      .campo {
        display: flex;
        flex-direction: column;
        gap: 0.5rem;
        margin-block-end: 1rem;
      }
      .campo label {
        font-weight: 600;
      }
      .campo input {
        min-height: var(--touch-target, 3.5rem);
        padding: 0 1rem;
        font: inherit;
        border: 2px solid #5f6368;
        border-radius: 0.75rem;
      }
      .campo .error {
        color: var(--color-danger, #e22931);
      }
    `,
    ],
    template: `
    <main class="pagina">
      <h1>Invitar cuidador</h1>

      @if (invitado()) {
        <section class="confirmacion" aria-labelledby="titulo-ok">
          <h2 id="titulo-ok">Cuidador agregado</h2>
          <p>{{ invitado() }} ya quedó vinculado a este adulto mayor.</p>
        </section>
        <a class="entrar" routerLink="/familiar">Volver al inicio</a>
      } @else {
        <form [formGroup]="formulario" (ngSubmit)="enviar()" novalidate>
          <div class="campo">
            <label for="nombre">Nombre</label>
            <input
              id="nombre"
              type="text"
              formControlName="first_name"
              autocomplete="off"
              [attr.aria-invalid]="invalido('first_name')"
            />
            @if (invalido('first_name')) {
              <span class="error" role="alert">Escribe el nombre.</span>
            }
          </div>

          <div class="campo">
            <label for="apellido">Apellido</label>
            <input
              id="apellido"
              type="text"
              formControlName="last_name"
              autocomplete="off"
              [attr.aria-invalid]="invalido('last_name')"
            />
            @if (invalido('last_name')) {
              <span class="error" role="alert">Escribe el apellido.</span>
            }
          </div>

          <div class="campo">
            <label for="correo">Correo electrónico</label>
            <input
              id="correo"
              type="email"
              formControlName="email"
              autocomplete="off"
              [attr.aria-invalid]="invalido('email')"
            />
            @if (invalido('email')) {
              <span class="error" role="alert">Escribe un correo válido.</span>
            }
          </div>

          <fieldset class="permisos" formGroupName="permissions">
            <legend>¿Qué puede hacer este cuidador?</legend>
            <label class="permiso">
              <input type="checkbox" formControlName="manage_contacts" />
              Gestionar contactos
            </label>
            <label class="permiso">
              <input type="checkbox" formControlName="manage_reminders" />
              Gestionar recordatorios
            </label>
            <label class="permiso">
              <input type="checkbox" formControlName="receive_alerts" />
              Recibir alertas de ayuda
            </label>
          </fieldset>

          @if (errorEnvio()) {
            <p class="error-general" role="alert">{{ errorEnvio() }}</p>
          }

          <button type="submit" class="entrar" [disabled]="enviando()">
            {{ enviando() ? 'Guardando…' : 'Invitar cuidador' }}
          </button>
        </form>
        <a class="enlace" routerLink="/familiar">Cancelar</a>
      }
    </main>
  `,
})
export class InvitarCuidador {
    private readonly ruta = inject(ActivatedRoute);
    private readonly servicio = inject(Cuidadores);
    private readonly auth = inject(Auth);

    private readonly adultoId = this.ruta.snapshot.paramMap.get('adultoId') ?? '';

    protected readonly enviando = signal(false);
    protected readonly errorEnvio = signal('');
    protected readonly invitado = signal('');

    protected readonly formulario = new FormGroup({
        first_name: new FormControl('', {
            nonNullable: true,
            validators: [Validators.required, Validators.pattern(/\S/)],
        }),
        last_name: new FormControl('', {
            nonNullable: true,
            validators: [Validators.required, Validators.pattern(/\S/)],
        }),
        email: new FormControl('', {
            nonNullable: true,
            validators: [Validators.required, Validators.email],
        }),
        permissions: new FormGroup({
            manage_contacts: new FormControl(false, { nonNullable: true }),
            manage_reminders: new FormControl(false, { nonNullable: true }),
            receive_alerts: new FormControl(false, { nonNullable: true }),
        }),
    });

    protected invalido(campo: 'first_name' | 'last_name' | 'email'): boolean {
        const c = this.formulario.controls[campo];
        return c.invalid && (c.touched || c.dirty);
    }

    protected enviar(): void {
        if (this.formulario.invalid) {
            this.formulario.markAllAsTouched();
            return;
        }
        const v = this.formulario.getRawValue();
        const datos: NuevoCuidador = {
            first_name: v.first_name.trim(),
            last_name: v.last_name.trim(),
            email: v.email.trim(),
            permissions: v.permissions,
        };

        this.enviando.set(true);
        this.errorEnvio.set('');
        this.servicio.invitar(this.adultoId, datos).subscribe({
            next: () => {
                this.invitado.set(`${datos.first_name} ${datos.last_name}`);
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
                        ? 'Solo el familiar responsable puede invitar cuidadores.'
                        : error.status === 409
                            ? 'Ya existe un cuidador con ese correo.'
                            : error.status === 422
                                ? 'Revisa los datos del cuidador.'
                                : 'No pudimos invitar al cuidador. Inténtalo de nuevo.',
                );
            },
        });
    }
}