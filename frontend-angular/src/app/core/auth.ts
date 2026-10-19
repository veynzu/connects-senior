import { HttpClient } from '@angular/common/http';
import { Injectable, computed, inject, signal } from '@angular/core';
import { Router } from '@angular/router';
import { Observable, tap } from 'rxjs';

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

@Injectable({ providedIn: 'root' })
export class Auth {
    private readonly http = inject(HttpClient);
    private readonly router = inject(Router);

    private readonly sesion = signal<SesionGuardada | null>(this.leerSesion());

    readonly usuario = computed(() => this.sesion()?.usuario ?? null);
    readonly token = computed(() => this.sesion()?.idToken ?? null);

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

    /** POST /api/auth/password-reset (contrato v1.0, A5): responde 204 exista o no el correo. */
    recuperar(email: string): Observable<void> {
        return this.http.post<void>(`${API_BASE_URL}/api/auth/password-reset`, { email });
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

    cerrarSesion(): void {
        this.limpiarSesion();
        this.router.navigateByUrl('/login');
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
            return Date.now() < sesion.expiraEn ? sesion : null;
        } catch {
            return null;
        }
    }
}