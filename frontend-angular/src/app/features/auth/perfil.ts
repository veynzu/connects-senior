import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, Injectable, inject, signal } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { Observable } from 'rxjs';

import { API_BASE_URL } from '../../core/api-config';
import { Auth } from '../../core/auth';

/*
 * Contrato v1.0, A6: PATCH /api/auth/me
 * Entrada: first_name, last_name, phone, photo_url (todos opcionales).
 * Este endpoint todavía NO existe en el backend.
 */
export interface CambiosPerfil {
    first_name?: string;
    last_name?: string;
    phone?: string;
}

@Injectable({ providedIn: 'root' })
export class Perfiles {
    private readonly http = inject(HttpClient);

    actualizar(cambios: CambiosPerfil): Observable<unknown> {
        return this.http.patch<unknown>(`${API_BASE_URL}/api/auth/me`, cambios);
    }
}

const ETIQUETAS_ROL: Record<string, string> = {
    adulto_mayor: 'Adulto mayor',
    familiar: 'Familiar responsable',
    cuidador: 'Cuidador',
    administrador: 'Administrador',
};

@Component({
    selector: 'app-perfil',
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
    `,
    ],
    template: `
    <main class="pagina">
      <h1>Mi perfil</h1>

      <section class="tarjeta" aria-label="Datos de tu cuenta">
        <p class="nombre">{{ nombre() }}</p>
        <p class="relacion">{{ correo() }}</p>
        <p class="etiqueta">{{ rol() }}</p>
      </section>

      <h2>Cambiar mis datos</h2>
      <p>Llena solo lo que quieras cambiar.</p>

      <form [formGroup]="formulario" (ngSubmit)="guardar()" novalidate>
        <div class="campo">
          <label for="nombre">Nombre</label>
          <input id="nombre" type="text" formControlName="first_name" autocomplete="given-name" />
        </div>
        <div class="campo">
          <label for="apellido">Apellido</label>
          <input
            id="apellido"
            type="text"
            formControlName="last_name"
            autocomplete="family-name"
          />
        </div>
        <div class="campo">
          <label for="telefono">Teléfono</label>
          <input id="telefono" type="tel" formControlName="phone" autocomplete="tel" />
        </div>

        @if (error()) {
          <p class="error-general" role="alert">{{ error() }}</p>
        }
        @if (guardado()) {
          <p class="confirmacion" role="status">
            Datos guardados. Verás el nombre nuevo la próxima vez que inicies sesión.
          </p>
        }

        <button type="submit" class="entrar" [disabled]="guardando()">
          {{ guardando() ? 'Guardando…' : 'Guardar cambios' }}
        </button>
      </form>

      <button type="button" class="entrar secundario" (click)="salir()">Cerrar sesión</button>
      <a class="enlace" [routerLink]="rutaVolver()">Volver</a>
    </main>
  `,
})
export class Perfil {
    private readonly auth = inject(Auth);
    private readonly servicio = inject(Perfiles);

    protected readonly guardando = signal(false);
    protected readonly guardado = signal(false);
    protected readonly error = signal('');

    protected readonly formulario = new FormGroup({
        first_name: new FormControl('', { nonNullable: true }),
        last_name: new FormControl('', { nonNullable: true }),
        phone: new FormControl('', { nonNullable: true }),
    });

    protected nombre(): string {
        const u = this.auth.usuario();
        return u?.display_name || u?.email || '';
    }

    protected correo(): string {
        return this.auth.usuario()?.email ?? '';
    }

    protected rol(): string {
        const rol = this.auth.usuario()?.role;
        return rol ? (ETIQUETAS_ROL[rol] ?? rol) : '';
    }

    protected rutaVolver(): string {
        return this.auth.rutaInicial();
    }

    protected guardar(): void {
        const v = this.formulario.getRawValue();
        const cambios: CambiosPerfil = {};
        if (v.first_name.trim()) {
            cambios.first_name = v.first_name.trim();
        }
        if (v.last_name.trim()) {
            cambios.last_name = v.last_name.trim();
        }
        if (v.phone.trim()) {
            cambios.phone = v.phone.trim();
        }

        this.guardado.set(false);
        if (Object.keys(cambios).length === 0) {
            this.error.set('Escribe al menos un dato para cambiar.');
            return;
        }

        this.guardando.set(true);
        this.error.set('');
        this.servicio.actualizar(cambios).subscribe({
            next: () => {
                this.formulario.reset();
                this.guardando.set(false);
                this.guardado.set(true);
            },
            error: (error: HttpErrorResponse) => {
                this.guardando.set(false);
                if (error.status === 401) {
                    this.auth.cerrarSesion();
                    return;
                }
                this.error.set(
                    error.status === 422
                        ? 'Revisa los datos que escribiste.'
                        : 'No pudimos guardar tus datos. Inténtalo de nuevo.',
                );
            },
        });
    }

    protected salir(): void {
        this.auth.cerrarSesion();
    }
}