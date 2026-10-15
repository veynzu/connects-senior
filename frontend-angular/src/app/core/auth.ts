import { HttpClient } from '@angular/common/http';
import { Injectable, computed, inject, signal } from '@angular/core';
import { Router } from '@angular/router';
import { Observable, tap } from 'rxjs';

import { API_BASE_URL } from './api-config';

/* Contrato tomado de backend-fastapi/app/models/auth.py (rama backend) */
export interface UsuarioAutenticado {
    uid: string;
    email: string | null;
    email_verified: boolean | null;
    display_name: string | null;
    photo_url: string | null;
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
        const nueva: SesionGuardada = {
            idToken: respuesta.id_token,
            refreshToken: respuesta.refresh_token,
            expiraEn: Date.now() + respuesta.expires_in * 1000,
            usuario: respuesta.user,
        };
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