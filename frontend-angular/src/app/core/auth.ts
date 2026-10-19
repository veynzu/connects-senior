import { HttpClient } from '@angular/common/http';
import { Injectable, computed, inject, signal } from '@angular/core';
import { Router } from '@angular/router';
import { Observable, finalize, map, shareReplay, tap, throwError } from 'rxjs';

import { API_BASE_URL } from './api-config';

export type Rol = 'adulto_mayor' | 'familiar' | 'cuidador' | 'administrador';

/* Contrato: backend-fastapi/app/models/auth.py (rama backend) + contrato v1.0 */
export interface UsuarioAutenticado {
    uid: string;
    email: string | null;
    email_verified: boolean | null;
    display_name: string | null;
    photo_url: string | null;
    /** Contrato v1.0: el backend todavía no lo entrega, por eso puede faltar. */
    role?: Rol | null;
}

export interface LoginRequest {
    email: string;
    password: string;
}

export interface LoginResponse {
    id_token: string;
    refresh_token: string;
    expires_in: number;
    user: UsuarioAutenticado;
}

/** Contrato v1.0, A4: POST /api/auth/register */
export interface DatosRegistro {
    first_name: string;
    last_name: string;
    email: string;
    password: string;
}

interface SesionGuardada {
    idToken: string;
    refreshToken: string;
    expiraEn: number;
    usuario: UsuarioAutenticado;
}

const CLAVE_SESION = 'connects-senior.sesion';

/** Inicio del detail con que el backend responde 403 cuando el correo no está verificado. */
export const PREFIJO_CORREO_SIN_VERIFICAR = 'Debes verificar tu correo';

@Injectable({ providedIn: 'root' })
export class Auth {
    private readonly http = inject(HttpClient);
    private readonly router = inject(Router);

    private readonly sesion = signal<SesionGuardada | null>(this.leerSesion());

    /** Renovación en curso: si varias peticiones fallan a la vez, todas esperan la misma. */
    private renovacion: Observable<string> | null = null;

    readonly usuario = computed(() => this.sesion()?.usuario ?? null);
    readonly token = computed(() => this.sesion()?.idToken ?? null);

    /** Aviso que muestra la pantalla de inicio de sesión (por ejemplo, verificar el correo). */
    private readonly aviso = signal('');
    readonly mensajeInicio = this.aviso.asReadonly();

    /** POST /api/auth/login */
    login(email: string, password: string): Observable<LoginResponse> {
        const cuerpo: LoginRequest = { email, password };
        return this.http
            .post<LoginResponse>(`${API_BASE_URL}/api/auth/login`, cuerpo)
            .pipe(tap((respuesta) => this.guardarSesion(respuesta)));
    }

    /** POST /api/auth/register (contrato v1.0, A4): crea un usuario familiar e inicia sesión. */
    registrar(datos: DatosRegistro): Observable<LoginResponse> {
        return this.http
            .post<LoginResponse>(`${API_BASE_URL}/api/auth/register`, datos)
            .pipe(tap((respuesta) => this.guardarSesion(respuesta)));
    }

    /**
     * POST /api/auth/register (contrato v1.0, A4) sin guardar la sesión: el backend no deja
     * entrar hasta que la persona verifique su correo, así que después del registro se va al login.
     */
    registrarSinSesion(datos: DatosRegistro): Observable<LoginResponse> {
        return this.http.post<LoginResponse>(`${API_BASE_URL}/api/auth/register`, datos);
    }

    /** POST /api/auth/password-reset (contrato v1.0, A5): responde 204 exista o no el correo. */
    recuperar(email: string): Observable<void> {
        return this.http.post<void>(`${API_BASE_URL}/api/auth/password-reset`, { email });
    }

    /**
     * POST /api/auth/refresh (contrato v1.0, A3): pide un token nuevo con el refresh_token
     * guardado y devuelve el id_token nuevo. Responde igual que el login (A1).
     */
    refrescar(): Observable<string> {
        const actual = this.sesion();
        if (!actual) {
            return throwError(() => new Error('No hay sesión para renovar.'));
        }
        if (!this.renovacion) {
            this.renovacion = this.http
                .post<LoginResponse>(`${API_BASE_URL}/api/auth/refresh`, {
                    refresh_token: actual.refreshToken,
                })
                .pipe(
                    tap((respuesta) =>
                        this.persistirSesion({
                            idToken: respuesta.id_token,
                            refreshToken: respuesta.refresh_token || actual.refreshToken,
                            expiraEn: Date.now() + respuesta.expires_in * 1000,
                            usuario: { ...actual.usuario, ...respuesta.user },
                        }),
                    ),
                    map((respuesta) => respuesta.id_token),
                    finalize(() => {
                        this.renovacion = null;
                    }),
                    shareReplay(1),
                );
        }
        return this.renovacion;
    }

    /**
     * GET /api/auth/me (contrato v1.0, A2): al recargar la página, recupera el usuario
     * y su rol desde el token guardado. Si no hay conexión, se conserva el usuario guardado.
     */
    recuperarUsuario(): void {
        if (!this.estaAutenticado()) {
            return;
        }
        this.http.get<UsuarioAutenticado>(`${API_BASE_URL}/api/auth/me`).subscribe({
            next: (usuario) => this.actualizarUsuario(usuario),
            error: () => {
                /* Sin conexión: se mantiene el usuario guardado. Un 401 lo maneja el interceptor. */
            },
        });
    }

    /** Pantalla a la que va cada rol después de iniciar sesión. */
    rutaInicial(): string {
        const rol = this.usuario()?.role;
        if (rol === 'administrador') {
            return '/admin';
        }
        return rol === 'familiar' || rol === 'cuidador' ? '/familiar' : '/inicio';
    }

    /** Actualiza datos del usuario guardado (por ejemplo, después de editar el perfil). */
    actualizarUsuario(cambios: Partial<UsuarioAutenticado>): void {
        const actual = this.sesion();
        if (!actual) {
            return;
        }
        this.persistirSesion({ ...actual, usuario: { ...actual.usuario, ...cambios } });
    }

    estaAutenticado(): boolean {
        const sesion = this.sesion();
        if (!sesion) {
            return false;
        }
        if (Date.now() >= sesion.expiraEn) {
            this.limpiarSesion();
            return false;
        }
        return true;
    }

    /** Cierra la sesión y va al login; `aviso`, si viene, se muestra allí. */
    cerrarSesion(aviso = ''): void {
        if (aviso) {
            this.aviso.set(aviso);
        }
        this.limpiarSesion();
        this.router.navigateByUrl('/login');
    }

    avisarEnInicio(texto: string): void {
        this.aviso.set(texto);
    }

    limpiarMensajeInicio(): void {
        this.aviso.set('');
    }

    private guardarSesion(respuesta: LoginResponse): void {
        this.persistirSesion({
            idToken: respuesta.id_token,
            refreshToken: respuesta.refresh_token,
            expiraEn: Date.now() + respuesta.expires_in * 1000,
            usuario: respuesta.user,
        });
    }

    private persistirSesion(nueva: SesionGuardada): void {
        this.sesion.set(nueva);
        try {
            localStorage.setItem(CLAVE_SESION, JSON.stringify(nueva));
        } catch {
            /* Sin almacenamiento disponible: la sesión dura mientras la página siga abierta. */
        }
    }

    private limpiarSesion(): void {
        this.sesion.set(null);
        try {
            localStorage.removeItem(CLAVE_SESION);
        } catch {
            /* Sin almacenamiento disponible. */
        }
    }

    private leerSesion(): SesionGuardada | null {
        try {
            const texto = localStorage.getItem(CLAVE_SESION);
            if (!texto) {
                return null;
            }
            const sesion = JSON.parse(texto) as SesionGuardada;
            return sesion;
        } catch {
            return null;
        }
    }
}
