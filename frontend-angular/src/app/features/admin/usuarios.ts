import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, Injectable, inject, signal } from '@angular/core';
import { Observable } from 'rxjs';

import { API_BASE_URL } from '../../core/api-config';
import { Auth } from '../../core/auth';

/*
 * Contrato v1.0, sección 7 (B1 y B2). Solo para el rol administrador.
 * Estos endpoints todavía NO existen en el backend.
 */
export interface UsuarioAdmin {
    uid: string;
    email: string;
    display_name: string | null;
    role: string;
    disabled: boolean;
}

const ETIQUETAS_ROL: Record<string, string> = {
    adulto_mayor: 'Adulto mayor',
    familiar: 'Familiar responsable',
    cuidador: 'Cuidador',
    administrador: 'Administrador',
};

@Injectable({ providedIn: 'root' })
export class Administracion {
    private readonly http = inject(HttpClient);

    /** GET /api/admin/users (B1) */
    listar(): Observable<UsuarioAdmin[]> {
        return this.http.get<UsuarioAdmin[]>(`${API_BASE_URL}/api/admin/users`);
    }

    /** PATCH /api/admin/users/{uid} (B2) */
    cambiarEstado(uid: string, disabled: boolean): Observable<UsuarioAdmin> {
        return this.http.patch<UsuarioAdmin>(`${API_BASE_URL}/api/admin/users/${uid}`, { disabled });
    }
}

type Estado = 'cargando' | 'error' | 'listo';

@Component({
    selector: 'app-usuarios-admin',
    imports: [],
    changeDetection: ChangeDetectionStrategy.OnPush,
    styleUrl: '../../shared/formulario.css',
    template: `
    <main class="pagina">
      <h1>Administración de usuarios</h1>

      @switch (estado()) {
        @case ('cargando') {
          <p role="status">Cargando usuarios…</p>
        }
        @case ('error') {
          <p class="error-general" role="alert">{{ mensajeError() }}</p>
          <button type="button" class="entrar secundario" (click)="cargar()">
            Intentar de nuevo
          </button>
        }
        @case ('listo') {
          @if (lista().length === 0) {
            <p>No hay usuarios registrados.</p>
          } @else {
            <ul class="lista">
              @for (u of lista(); track u.uid) {
                <li class="tarjeta">
                  <p class="nombre">{{ nombre(u) }}</p>
                  <p class="relacion">{{ u.email }}</p>
                  <p class="relacion">{{ rol(u) }}</p>
                  <p class="etiqueta">{{ u.disabled ? 'Deshabilitado' : 'Activo' }}</p>

                  @if (u.uid !== miUid()) {
                    @if (confirmando() === u.uid) {
                      <p role="alert">¿Seguro que quieres deshabilitar esta cuenta?</p>
                      <button
                        type="button"
                        class="entrar"
                        [disabled]="ocupado()"
                        (click)="cambiar(u, true)"
                      >
                        {{ ocupado() ? 'Guardando…' : 'Sí, deshabilitar' }}
                      </button>
                      <button
                        type="button"
                        class="entrar secundario"
                        [disabled]="ocupado()"
                        (click)="confirmando.set('')"
                      >
                        No, cancelar
                      </button>
                    } @else if (u.disabled) {
                      <button
                        type="button"
                        class="entrar"
                        [disabled]="ocupado()"
                        [attr.aria-label]="'Habilitar la cuenta de ' + u.email"
                        (click)="cambiar(u, false)"
                      >
                        Habilitar cuenta
                      </button>
                    } @else {
                      <button
                        type="button"
                        class="entrar secundario"
                        [disabled]="ocupado()"
                        [attr.aria-label]="'Deshabilitar la cuenta de ' + u.email"
                        (click)="confirmando.set(u.uid)"
                      >
                        Deshabilitar cuenta
                      </button>
                    }
                  }
                </li>
              }
            </ul>
          }
          @if (errorAccion()) {
            <p class="error-general" role="alert">{{ errorAccion() }}</p>
          }
        }
      }

      <button type="button" class="entrar secundario" (click)="salir()">Cerrar sesión</button>
    </main>
  `,
})
export class UsuariosAdmin {
    private readonly servicio = inject(Administracion);
    private readonly auth = inject(Auth);

    protected readonly estado = signal<Estado>('cargando');
    protected readonly lista = signal<UsuarioAdmin[]>([]);
    protected readonly mensajeError = signal('');
    protected readonly confirmando = signal('');
    protected readonly ocupado = signal(false);
    protected readonly errorAccion = signal('');

    constructor() {
        this.cargar();
    }

    protected miUid(): string {
        return this.auth.usuario()?.uid ?? '';
    }

    protected nombre(u: UsuarioAdmin): string {
        return u.display_name || u.email;
    }

    protected rol(u: UsuarioAdmin): string {
        return ETIQUETAS_ROL[u.role] ?? u.role;
    }

    protected cargar(): void {
        this.estado.set('cargando');
        this.servicio.listar().subscribe({
            next: (usuarios) => {
                this.lista.set(usuarios);
                this.estado.set('listo');
            },
            error: (error: HttpErrorResponse) => {
                if (error.status === 401) {
                    this.auth.cerrarSesion();
                    return;
                }
                this.mensajeError.set(
                    error.status === 403
                        ? 'Solo el administrador puede ver esta página.'
                        : 'No pudimos cargar los usuarios. Revisa tu conexión e inténtalo de nuevo.',
                );
                this.estado.set('error');
            },
        });
    }

    protected cambiar(u: UsuarioAdmin, disabled: boolean): void {
        this.ocupado.set(true);
        this.errorAccion.set('');
        this.servicio.cambiarEstado(u.uid, disabled).subscribe({
            next: (actualizado) => {
                this.lista.update((l) =>
                    l.map((x) => (x.uid === u.uid ? { ...x, ...actualizado, disabled } : x)),
                );
                this.confirmando.set('');
                this.ocupado.set(false);
            },
            error: (error: HttpErrorResponse) => {
                this.ocupado.set(false);
                this.confirmando.set('');
                if (error.status === 401) {
                    this.auth.cerrarSesion();
                    return;
                }
                this.errorAccion.set(
                    error.status === 403
                        ? 'No tienes permiso para cambiar esta cuenta.'
                        : 'No pudimos cambiar el estado de la cuenta. Inténtalo de nuevo.',
                );
            },
        });
    }

    protected salir(): void {
        this.auth.cerrarSesion();
    }
}