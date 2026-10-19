import { HttpErrorResponse, HttpInterceptorFn, HttpRequest } from '@angular/common/http';
import { inject } from '@angular/core';
import { catchError, switchMap, throwError } from 'rxjs';

import { API_BASE_URL } from './api-config';
import { Auth } from './auth';

/** Rutas que no llevan token ni se reintentan: el login mismo, el registro y la renovación. */
const RUTAS_SIN_TOKEN = [
    '/api/auth/login',
    '/api/auth/register',
    '/api/auth/refresh',
    '/api/auth/password-reset',
];

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
            if (!(error instanceof HttpErrorResponse) || error.status !== 401) {
                return throwError(() => error);
            }
            // 401: el token venció. Se renueva una vez y se repite la petición.
            return auth.refrescar().pipe(
                catchError(() => {
                    auth.cerrarSesion();
                    return throwError(() => error);
                }),
                switchMap((tokenNuevo) => siguiente(conToken(peticion, tokenNuevo))),
            );
        }),
    );
};