import { HttpInterceptorFn } from '@angular/common/http';
import { inject } from '@angular/core';

import { API_BASE_URL } from './api-config';
import { Auth } from './auth';

/** Agrega "Authorization: Bearer <token>" a las peticiones dirigidas al backend. */
export const authInterceptor: HttpInterceptorFn = (peticion, siguiente) => {
    const token = inject(Auth).token();
    if (token && peticion.url.startsWith(API_BASE_URL)) {
        peticion = peticion.clone({ setHeaders: { Authorization: `Bearer ${token}` } });
    }
    return siguiente(peticion);
};
