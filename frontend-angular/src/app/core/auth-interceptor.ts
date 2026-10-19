import { HttpErrorResponse, HttpInterceptorFn, HttpRequest } from '@angular/common/http';
import { inject } from '@angular/core';
import { catchError, switchMap, throwError } from 'rxjs';

import { API_BASE_URL } from './api-config';
import { Auth, PREFIJO_CORREO_SIN_VERIFICAR } from './auth';
import { detalleError } from './errores';

/** Rutas que no llevan token ni se reintentan: el login mismo, el registro y la renovación. */
const RUTAS_SIN_TOKEN = [
    '/api/auth/login',
    '/api/auth/register',
    '/api/auth/refresh',
    '/api/auth/password-reset',
];

/** detail del 403 "Debes verificar tu correo…" del backend, o texto vacío si es otro error. */
function avisoCorreoSinVerificar(error: unknown): string {
    if (!(error instanceof HttpErrorResponse) || error.status !== 403) {
        return '';
    }
    const detalle = detalleError(error);
    return detalle.startsWith(PREFIJO_CORREO_SIN_VERIFICAR) ? detalle : '';
}

function conToken(peticion: HttpRequest<unknown>, token: string | null): HttpRequest<unknown> {
    return token ? peticion.clone({ setHeaders: { Authorization: `Bearer ${token}` } }) : peticion;
}

export const authInterceptor: HttpInterceptorFn = (peticion, siguiente) => {
    if (!peticion.url.startsWith(API_BASE_URL)) {
        return siguiente(peticion);
    }
    if (RUTAS_SIN_TOKEN.some((ruta) => peticion.url.startsWith(`${API_BASE_URL}${ruta}`))) {
        return siguiente(peticion);
    }

    const auth = inject(Auth);

    return siguiente(conToken(peticion, auth.token())).pipe(
        catchError((error: unknown) => {
            // 403 por correo sin verificar: se cierra la sesión y el login muestra el aviso.
            const aviso = avisoCorreoSinVerificar(error);
            if (aviso) {
                auth.cerrarSesion(aviso);
                return throwError(() => error);
            }
            if (!(error instanceof HttpErrorResponse) || error.status !== 401) {
                return throwError(() => error);
            }
            // 401: el token venció. Se renueva una vez y se repite la petición.
            return auth.refrescar().pipe(
                catchError((errorRenovacion: unknown) => {
                    auth.cerrarSesion(avisoCorreoSinVerificar(errorRenovacion));
                    return throwError(() => error);
                }),
                switchMap((tokenNuevo) => siguiente(conToken(peticion, tokenNuevo))),
            );
        }),
    );
};
